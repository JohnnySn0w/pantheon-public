"""T-2919 private-bus tier: the approval service and the Telegram ingress
as two real subprocesses on one private dbus-daemon, the ingress talking
to the in-test-process loopback fake Bot API.

Proves over a real bus (gates 14 / 21 / 22 + the uid gates):

- red spoofs then the green real callback, end to end
  (RequestHuman -> Present -> sendMessage -> callback -> HumanDecision);
- ``Present`` is callable only by the approval uid and ``HumanDecision``
  only by the ingress uid — both tested as NEGATIVES with every field
  correct (relative-uid scenarios; the bus is deliberately permissive so
  every refusal is the services' own server-side check);
- leak canaries over /proc/<pid>/cmdline + environ of both processes,
  both service logs, and a ``dbus-monitor``
  capture of all bus traffic (no signal carries anything; the nonce rides
  only the two unicast private calls).

Skips without dbus-python / dbus-daemon (the main ci.yml tools suite);
runs in the dbus-tiers workflow (glob ``tools/test_host_control_*.py``),
which forbids skips: nothing here may depend on the mcp-2 adapter runtime.
The live-adapter leg lives in tools/test_approval_mcp_adapter_telegram_ingress.py
(T-4858 runtime class).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.host_control.approval import wire
from tools.host_control.telegram_ingress import dbus_service as ingress_dbus
from tools.host_control.telegram_ingress import wire as ingress_wire
from tools.test_host_control_telegram_ingress_fixtures import (
    CANARY_DIGEST,
    CANARY_TOKEN,
    CANARY_TOKEN_TEXT,
    EFFECT_CANARY,
    HUMAN_CHAT,
    HUMAN_USER,
    NONCE_MARK,
    OTHER_USER,
    PLAN_HANDLE,
    FakeBotApi,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DBUS_DAEMON = shutil.which("dbus-daemon")
DBUS_MONITOR = shutil.which("dbus-monitor")

requires_private_bus = unittest.skipUnless(
    ingress_dbus.DBUS_AVAILABLE and DBUS_DAEMON and DBUS_MONITOR,
    "private-bus integration needs dbus-python, dbus-daemon and dbus-monitor",
)

_PERMISSIVE_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def _wait(predicate, timeout: float = 30.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


@requires_private_bus
class _IngressBusCase(unittest.TestCase):
    INGRESS_OFFSET = 0  # approval side: registered ingress uid = caller + this
    APPROVAL_OFFSET = 0  # ingress side: allowed Present caller = caller + this
    # the agent approver ('specialist') uid = caller + this. It may never equal
    # the ingress uid (the service refuses, review finding 7); when it is
    # not the caller, the approval DRIVER acts as specialist in-process (opens
    # the review, serves file-triggered RequestHuman) and the test reads
    # outcomes from the approval journal.
    AGENT_OFFSET = 5000
    # T-5518 scenario knobs: the approval driver role ("approval" = the
    # real service; "held_approval" = a stub that holds HumanDecision),
    # how many reviews the driver opens, and its Present timeout
    APPROVAL_ROLE = "approval"
    DRIVER_REVIEWS = 1
    PRESENT_TIMEOUT_SECONDS = 10.0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        import tools._host_control_testbus as testbus

        cls.testbus = testbus
        cls._processes: list[subprocess.Popen] = []
        cls._logs = []
        cls._bus_handle = testbus.spawn_daemon("t2919-", _PERMISSIVE_BUS_CONFIG)
        cls.bus_address = cls._bus_handle.bus_address
        cls.work = tempfile.TemporaryDirectory()
        cls.root = Path(cls.work.name)
        cls.root.chmod(0o700)
        cls.fake = FakeBotApi()
        monitor_log = open(cls.root / "monitor.log", "w", encoding="utf-8")  # noqa: SIM115
        cls._logs.append(monitor_log)
        cls.monitor = subprocess.Popen(
            [DBUS_MONITOR, "--address", cls.bus_address, "--monitor"],
            stdout=monitor_log, stderr=subprocess.STDOUT,
            preexec_fn=testbus.pdeathsig_preexec_fn(),
        )
        cls._processes.append(cls.monitor)
        _wait(lambda: (cls.root / "monitor.log").stat().st_size > 0, what="dbus-monitor")

        import dbus.bus

        cls.connection = dbus.bus.BusConnection(cls.bus_address)
        caller = os.getuid()
        # fixture credential (ORG-051 host-file lease): 0700 dir, 0600 file
        cred = cls.root / "cred"
        cred.mkdir(mode=0o700)
        cred.chmod(0o700)
        (cred / "bot-token").write_bytes(CANARY_TOKEN + b"\n")
        (cred / "bot-token").chmod(0o600)
        state_dir = cls.root / "ingress-state"
        state_dir.mkdir(mode=0o700)
        ingress_config = cls.root / "ingress.json"
        ingress_config.write_text(json.dumps({
            "schema": "pantheon-telegram-ingress-config/v1",
            "ingress_uid": caller,
            "approval_uid": caller + 9999,  # placeholder: must differ from ingress_uid
            "credential": {"credential_id": "telegram-approval-bot",
                           "location": str(cred / "bot-token"), "owner_gid": os.getgid()},
            "allowlist": {"user_id": HUMAN_USER, "chat_id": HUMAN_CHAT},
            "state_dir": str(state_dir),
            "card_ttl_max_seconds": 3600,
            "poll_timeout_seconds": 0,
        }), encoding="utf-8")
        ingress_config.chmod(0o644)
        cls.approval_root = cls.root / "approval"
        ready_file = "held-ready" if cls.APPROVAL_ROLE == "held_approval" else "state.json"
        cls.approval_proc = cls._spawn("approval", {
            "role": cls.APPROVAL_ROLE, "bus_address": cls.bus_address,
            "root": str(cls.approval_root), "ingress_offset": cls.INGRESS_OFFSET,
            "agent_offset": cls.AGENT_OFFSET, "driver_review": cls.AGENT_OFFSET != 0,
            "driver_review_count": cls.DRIVER_REVIEWS,
            "present_timeout_seconds": cls.PRESENT_TIMEOUT_SECONDS})
        _wait(lambda: cls.connection.name_has_owner(wire.BUS_NAME)
              and (cls.approval_root / ready_file).exists(), what="approval name")
        cls.approval_state = (
            json.loads((cls.approval_root / "state.json").read_text(encoding="utf-8"))
            if ready_file == "state.json" else {})
        cls.ingress_proc = cls._spawn("ingress", {
            "role": "ingress", "bus_address": cls.bus_address, "root": str(cls.root),
            "ingress_config": str(ingress_config), "api_base": cls.fake.base_url,
            "present_caller_offset": cls.APPROVAL_OFFSET})
        try:
            _wait(lambda: (cls.root / "ingress-ready").exists()
                  and cls.connection.name_has_owner(ingress_wire.INGRESS_BUS_NAME),
                  what="ingress ready")
        except AssertionError:
            log = (cls.root / "ingress.log").read_text(encoding="utf-8")[-3000:]
            cls.tearDownClass()
            raise AssertionError(f"ingress never became ready; log tail:\n{log}") from None

    @classmethod
    def _spawn(cls, name: str, config: dict) -> subprocess.Popen:
        config_path = cls.root / f"{name}-driver.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        log = open(cls.root / f"{name}.log", "w", encoding="utf-8")  # noqa: SIM115
        cls._logs.append(log)
        process = subprocess.Popen(
            [sys.executable, "-m", "tools.test_host_control_telegram_ingress_driver", str(config_path)],
            cwd=REPO_ROOT, stdout=log, stderr=log,
            preexec_fn=cls.testbus.pdeathsig_preexec_fn(),
        )
        cls._bus_handle.register_driver(process.pid)
        cls._processes.append(process)
        return process

    @classmethod
    def tearDownClass(cls) -> None:
        for process in reversed(getattr(cls, "_processes", [])):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        for log in getattr(cls, "_logs", []):
            log.close()
        if getattr(cls, "fake", None) is not None:
            cls.fake.close()
        if getattr(cls, "_bus_handle", None) is not None:
            cls.testbus.stop_daemon(cls._bus_handle)
        if getattr(cls, "work", None) is not None:
            cls.work.cleanup()
        super().tearDownClass()

    # helpers -------------------------------------------------------------

    def call(self, member: str, signature: str, args: tuple, *, interface: str = wire.REVIEWER_INTERFACE,
             bus_name: str = wire.BUS_NAME, path: str = wire.OBJECT_PATH):
        return self.connection.call_blocking(bus_name, path, interface, member, signature, args)

    def assert_denied(self, expected_name: str, *call_args, **call_kwargs):
        import dbus

        with self.assertRaises(dbus.exceptions.DBusException) as caught:
            self.call(*call_args, **call_kwargs)
        self.assertEqual(caught.exception.get_dbus_name(), expected_name)
        return caught.exception

    def open_review(self) -> str:
        if self.AGENT_OFFSET:
            return self.approval_state["review_handle"]
        handle, state, _digest, _summary = self.call("Review", "sss", (PLAN_HANDLE, CANARY_DIGEST, ""))
        self.assertEqual(str(state), "open")
        return str(handle)

    def request_human(self, review: str, key: str) -> tuple[str, str]:
        if not self.AGENT_OFFSET:
            handle, state = self.call("RequestHuman", "sss", (review, CANARY_DIGEST, key))
            return str(handle), str(state)
        name = f"{key}-{time.monotonic_ns()}"
        trigger = self.approval_root / f"trigger-{name}.json"
        tmp = trigger.with_suffix(".tmp")
        tmp.write_text(json.dumps({"review_handle": review, "digest": CANARY_DIGEST, "key": key}),
                       encoding="utf-8")
        tmp.rename(trigger)
        result = self.approval_root / f"result-{name}.json"
        _wait(result.exists, what=f"driver RequestHuman {key}")
        body = json.loads(result.read_text(encoding="utf-8"))
        self.assertNotIn("error", body, body)
        return body["request_handle"], body["state"]

    def journal_rows(self, sql: str) -> list:
        with sqlite3.connect(self.approval_root / "approval.sqlite3") as connection:
            return connection.execute(sql).fetchall()

    def nonce_for(self, review_handle: str) -> str:
        return self.journal_rows(
            f"SELECT nonce FROM human_requests WHERE review_handle='{review_handle}'")[0][0]

    def review_state(self, review_handle: str) -> tuple[str, dict]:
        """Authoritative state straight from the approval journal (the
        test process is not the approver when AGENT_OFFSET != 0)."""

        state, authority, approver = self.journal_rows(
            "SELECT state, authority_source, approver_subject FROM reviews "
            f"WHERE review_handle='{review_handle}'")[0]
        grants = self.journal_rows(f"SELECT approval_handle FROM grants WHERE review_handle='{review_handle}'")
        human = self.journal_rows(f"SELECT state FROM human_requests WHERE review_handle='{review_handle}'")
        return state, {
            "authority_source": authority,
            "approver_subject": approver,
            "approval_handle": grants[0][0] if grants else None,
            "human_request": {"state": human[0][0]} if human else None,
        }

    def log_text(self, name: str) -> str:
        return (self.root / f"{name}.log").read_text(encoding="utf-8")

    def wait_answered(self, callback_id: str) -> None:
        _wait(lambda: callback_id in self.fake.answered, what=f"answer to {callback_id}")


class EndToEndBusTests(_IngressBusCase):
    def test_red_spoofs_then_green_callback_then_no_leaks_anywhere(self) -> None:
        review = self.open_review()
        request_handle, state = self.request_human(review, "mbx-1")
        self.assertEqual(str(state), "presented")
        self.assertTrue(str(request_handle).startswith(NONCE_MARK))
        # a replayed mailbox request: same handle, no second card
        again = self.request_human(review, "mbx-1")
        self.assertEqual(str(again[0]), str(request_handle))
        self.assertEqual(len(self.fake.messages), 1)
        card = self.fake.last_card()
        approve = self.fake.button_data(card, 0)
        # RED: wrong user, forwarded prose, edited card
        _, wrong = self.fake.callback(message=card, data=approve, sender=self.fake.user(OTHER_USER))
        self.wait_answered(wrong)
        self.fake.push("message", {"message_id": 9100, "date": 1, "chat": self.fake.chat(),
                                   "from": self.fake.user(), "text": "approved",
                                   "forward_origin": {"type": "user", "date": 1,
                                                      "sender_user": self.fake.user()}})
        edited = dict(card, text=card["text"] + " OK")
        _, edited_id = self.fake.callback(message=edited, data=approve)
        self.wait_answered(edited_id)
        _wait(lambda: not self.fake.pending_update_ids() or True, what="drain")
        self.assertEqual(self.review_state(review)[0], "open")
        # GREEN
        _, real = self.fake.callback(message=self.fake.card_message(card["chat"]["id"], card["message_id"]),
                                     data=approve)
        _wait(lambda: self.review_state(review)[0] == "granted", what="human grant")
        self.wait_answered(real)
        state, summary = self.review_state(review)
        self.assertEqual(summary["authority_source"], "human")
        self.assertEqual(summary["approver_subject"], "operator")
        self.assertRegex(summary["approval_handle"], r"^[0-9a-f]{64}$")
        self.assertEqual(summary["human_request"]["state"], "decided")
        nonce = self.nonce_for(review)
        self.assertEqual(self.journal_rows("SELECT count(*) FROM provider_attestations")[0][0], 1)
        # replay of the real callback: refused, still one attestation
        self.fake.callback(message=card, data=approve, callback_id=real)
        _, again_id = self.fake.callback(message=card, data=approve)
        self.wait_answered(again_id)
        self.assertEqual(self.journal_rows("SELECT count(*) FROM provider_attestations")[0][0], 1)

        # ---- E21: leak canaries over every process-level sink ----
        forbidden = {"token": CANARY_TOKEN_TEXT, "nonce": nonce, "callback data": approve,
                     "digest": CANARY_DIGEST, "effects": EFFECT_CANARY}
        for proc in (self.approval_proc, self.ingress_proc):
            for sink in ("cmdline", "environ"):
                with self.subTest(pid=proc.pid, sink=sink):
                    raw = Path(f"/proc/{proc.pid}/{sink}").read_bytes().decode(errors="replace")
                    self.assertEqual([k for k, v in forbidden.items() if v in raw], [])
        ingress_log = self.log_text("ingress")
        self.assertIn("event=decision_recorded", ingress_log)
        self.assertEqual([k for k, v in forbidden.items() if v in ingress_log], [])
        approval_log = self.log_text("approval")
        self.assertEqual([k for k in ("token", "nonce", "callback data")
                          if forbidden[k] in approval_log], [])
        # bus traffic: no signal carries anything sensitive; the nonce
        # rides ONLY the two unicast private calls; the token never
        monitor = self.log_text("monitor")
        self.assertNotIn(CANARY_TOKEN_TEXT, monitor)
        blocks = [b for b in monitor.split("\n\n") if b.strip()]
        blocks = []
        current: list[str] = []
        for line in monitor.splitlines():
            if line and not line.startswith(" "):
                if current:
                    blocks.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            blocks.append("\n".join(current))
        nonce_blocks = [b for b in blocks if nonce in b]
        self.assertTrue(nonce_blocks, "the Present/HumanDecision calls were not captured")
        for block in nonce_blocks:
            with self.subTest(block=block.splitlines()[0][:120]):
                header = block.splitlines()[0]
                self.assertTrue(header.startswith("method call"))
                self.assertTrue("member=Present" in header or "member=HumanDecision" in header)
        for block in blocks:
            if block.startswith("signal"):
                with self.subTest(signal=block.splitlines()[0][:120]):
                    self.assertEqual([k for k, v in forbidden.items() if v in block], [])

    def test_present_and_human_decision_are_each_exactly_one_method_no_signals(self) -> None:
        from xml.etree import ElementTree

        xml = self.call("Introspect", "", (), interface="org.freedesktop.DBus.Introspectable",
                        bus_name=ingress_wire.INGRESS_BUS_NAME, path=ingress_wire.INGRESS_OBJECT_PATH)
        tree = ElementTree.fromstring(str(xml))
        ours = [i for i in tree.findall("interface") if i.get("name") == ingress_wire.INGRESS_INTERFACE]
        self.assertEqual(len(ours), 1)
        self.assertEqual([m.get("name") for m in ours[0].findall("method")], ["Present"])
        self.assertEqual([a.get("type") for a in ours[0].find("method").findall("arg")],
                         ["s"] * 7)
        self.assertEqual(tree.findall(".//signal"), [])
        self.assertEqual([i.get("name") for i in tree.findall("interface")
                          if i.get("name").startswith("org.pantheon")], [ingress_wire.INGRESS_INTERFACE])


class PresentCallerGateBusTests(_IngressBusCase):
    """The ingress allows Present only from caller+4000: the real approval
    service (caller uid) and the test process are both refused."""

    APPROVAL_OFFSET = 4000

    def test_present_from_any_other_uid_is_denied_with_correct_fields(self) -> None:
        summary = json.dumps({
            "schema": ingress_wire.CARD_SCHEMA, "review_handle": "a" * 64, "plan_digest": "b" * 64,
            "target_seat_id": "recovery", "operation_families": ["account.create"],
            "redacted_effects": "x", "expires_at": "2099-01-01T00:00:00+00:00"})
        self.assert_denied(ingress_wire.INGRESS_POLICY_DENIED_ERROR, "Present", "sssss",
                           ("a" * 64, "b" * 64, summary, "c" * 64, "2099-01-01T00:00:00+00:00"),
                           interface=ingress_wire.INGRESS_INTERFACE,
                           bus_name=ingress_wire.INGRESS_BUS_NAME, path=ingress_wire.INGRESS_OBJECT_PATH)
        review = self.open_review()
        _handle, state = self.request_human(review, "k")
        self.assertEqual(str(state), "pending")
        self.assertEqual(self.fake.messages, {})


class HumanDecisionCallerGateBusTests(_IngressBusCase):
    """The approval service registers caller+3000 as the ingress uid: the
    real ingress process (caller uid) and the test process are refused,
    even with the correct nonce."""

    INGRESS_OFFSET = 3000
    AGENT_OFFSET = 0  # the test process IS the approver here (ingress uid differs)

    def test_human_decision_from_any_other_uid_is_denied_with_correct_fields(self) -> None:
        review = self.open_review()
        _handle, state = self.call("RequestHuman", "sss", (review, CANARY_DIGEST, "k"))
        self.assertEqual(str(state), "presented")
        nonce = self.nonce_for(review)
        error = self.assert_denied(wire.POLICY_DENIED_ERROR, "HumanDecision", "ssssss",
                                   (review, CANARY_DIGEST, "granted", "src", nonce, "k1"),
                                   interface=wire.HUMAN_INGRESS_INTERFACE)
        self.assertEqual(error.get_dbus_message(), "subject_denied")
        card = self.fake.last_card()
        _, cb = self.fake.callback(message=card, data=self.fake.button_data(card, 0))
        self.wait_answered(cb)
        _wait(lambda: "event=decision_refused" in self.log_text("ingress"), what="refusal logged")
        self.assertIn("detail=subject_denied", self.log_text("ingress"))
        self.assertEqual(self.review_state(review)[0], "open")
        self.assertEqual(self.journal_rows("SELECT count(*) FROM provider_attestations")[0][0], 0)


# ---------------------------------------------------------------------------
# T-5518: neither service's loop blocks on the other.
#
# Every scenario below has a DETERMINISTIC old-code failure mode far from
# its bound: with a synchronous Present or HumanDecision the probed call
# cannot complete until a 10 s D-Bus timeout (or the fake's 8 s getUpdates
# hold) fires, while the async code completes in well under a second.
# ---------------------------------------------------------------------------

# the bound sits between "async: < 1 s" and "sync: >= 8 s" with margin both
# ways (a loaded host); the measured spread is in the T-5518 report
NONBLOCKING_BOUND_SECONDS = 4.0


def _present_args(review_handle: str) -> tuple[str, str, str, str, str]:
    from datetime import datetime, timedelta, timezone

    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    summary = json.dumps({
        "schema": ingress_wire.CARD_SCHEMA, "review_handle": review_handle,
        "plan_digest": CANARY_DIGEST, "target_seat_id": "recovery",
        "operation_families": ["account.create"], "redacted_effects": "x",
        "expires_at": expires})
    nonce = NONCE_MARK + review_handle[len(NONCE_MARK):][::-1]
    return (review_handle, CANARY_DIGEST, summary, nonce, expires)


class _AsyncBusMixin:
    def trigger_request(self, review: str, key: str, digest: str = CANARY_DIGEST) -> Path:
        """File-triggered driver RequestHuman WITHOUT waiting for it."""

        name = f"{key}-{time.monotonic_ns()}"
        trigger = self.approval_root / f"trigger-{name}.json"
        tmp = trigger.with_suffix(".tmp")
        tmp.write_text(json.dumps({"review_handle": review, "digest": digest, "key": key}),
                       encoding="utf-8")
        tmp.rename(trigger)
        return self.approval_root / f"result-{name}.json"

    def present_seen(self, review: str) -> bool:
        """dbus-monitor captured a Present method call for ``review``."""

        blocks = self.log_text("monitor").split("\nmethod call")
        return any("member=Present" in b.splitlines()[0] and review in b
                   for b in blocks if b.strip())

    def park_ingress_in_get_updates(self):
        """Hold the ingress loop inside a getUpdates (Bot API, on-loop by
        DECISION 3) so a Present sent now stays in flight."""

        import threading

        gate = threading.Event()
        polls = self.fake.held_polls
        self.fake.hold_updates = gate
        return gate, polls


class ConcurrentPresentAndHumanDecisionBusTests(_AsyncBusMixin, _IngressBusCase):
    """Ask 5(1): RequestHuman B (approval -> Present) and HumanDecision A
    (ingress -> approval) in flight at the SAME time. Old code: the
    approval blocks in Present B while the ingress blocks in HumanDecision
    A, so neither completes before the 10 s Present timeout."""

    DRIVER_REVIEWS = 2

    def test_concurrent_request_human_and_human_decision_complete_without_timeout(self) -> None:
        review_a, review_b = self.approval_state["review_handles"]
        _handle, state = self.request_human(review_a, "conc-a")
        self.assertEqual(state, "presented")
        card_a = self.fake.last_card()
        gate, polls = self.park_ingress_in_get_updates()
        try:
            _, tap = self.fake.callback(message=card_a, data=self.fake.button_data(card_a, 0))
            _wait(lambda: self.fake.held_polls > polls, what="ingress parked in getUpdates")
            result_b = self.trigger_request(review_b, "conc-b", self.approval_state["review_digests"][1])
            _wait(lambda: self.present_seen(review_b), what="Present B in flight")
            started = time.monotonic()
            gate.set()  # the ingress now sends HumanDecision A
        finally:
            self.fake.hold_updates = None
            gate.set()
        _wait(lambda: self.review_state(review_a)[0] == "granted", what="grant A")
        granted_after = time.monotonic() - started
        _wait(result_b.exists, what="RequestHuman B settled")
        presented_after = time.monotonic() - started
        print(f"T5518-TIMING concurrent granted={granted_after:.3f}s presented={presented_after:.3f}s",
              file=sys.stderr)
        self.assertLess(granted_after, NONBLOCKING_BOUND_SECONDS)
        self.assertLess(presented_after, NONBLOCKING_BOUND_SECONDS)
        self.assertEqual(json.loads(result_b.read_text(encoding="utf-8"))["state"], "presented")
        self.wait_answered(tap)
        # exactly one decision, one card per review, no timeout anywhere
        self.assertEqual(self.journal_rows("SELECT count(*) FROM provider_attestations")[0][0], 1)
        self.assertEqual(self.review_state(review_b)[1]["human_request"]["state"], "presented")
        self.assertEqual(len(self.fake.messages), 2)
        self.assertNotIn("decision_unavailable", self.log_text("ingress"))


class ApprovalLoopServesWhilePresentInFlightBusTests(_AsyncBusMixin, _IngressBusCase):
    """The approval side alone: a Reviewer call completes promptly while
    its RequestHuman's Present is held at the parked ingress (old code:
    the service's loop sits in the synchronous Present until the ingress
    is released by the fake's 8 s hold)."""

    INGRESS_OFFSET = 3000
    AGENT_OFFSET = 0  # the test process IS the approver here

    def test_reviewer_call_completes_while_present_is_in_flight(self) -> None:
        import threading

        import dbus.bus

        review = self.open_review()
        probe = self.open_review()
        gate, polls = self.park_ingress_in_get_updates()
        out: dict = {}
        try:
            self.fake.push("message", {"message_id": 9200, "date": 1, "chat": self.fake.chat(),
                                       "from": self.fake.user(), "text": "park"})
            _wait(lambda: self.fake.held_polls > polls, what="ingress parked in getUpdates")

            def request() -> None:
                connection = dbus.bus.BusConnection(self.bus_address)
                try:
                    out["reply"] = connection.call_blocking(
                        wire.BUS_NAME, wire.OBJECT_PATH, wire.REVIEWER_INTERFACE,
                        "RequestHuman", "sss", (review, CANARY_DIGEST, "live-1"), timeout=30)
                finally:
                    connection.close()

            worker = threading.Thread(target=request)
            worker.start()
            _wait(lambda: self.present_seen(review), what="Present in flight")
            started = time.monotonic()
            state, _summary = self.call("ReviewGet", "s", (probe,))
            elapsed = time.monotonic() - started
            self.assertEqual(str(state), "open")
        finally:
            self.fake.hold_updates = None
            gate.set()
        worker.join(30)
        print(f"T5518-TIMING approval-loop reviewget={elapsed:.3f}s", file=sys.stderr)
        self.assertLess(elapsed, NONBLOCKING_BOUND_SECONDS)
        self.assertEqual(str(out["reply"][1]), "presented")


class IngressLoopServesPresentWhileDecisionInFlightBusTests(_AsyncBusMixin, _IngressBusCase):
    """The ingress side alone, against a stub approval service that HOLDS
    every HumanDecision reply: Present stays servable while a decision is
    in flight (old code: the ingress tick sits in the synchronous
    HumanDecision until its 10 s timeout)."""

    APPROVAL_ROLE = "held_approval"

    def present(self, review: str):
        return self.call("Present", "sssss", _present_args(review),
                         interface=ingress_wire.INGRESS_INTERFACE,
                         bus_name=ingress_wire.INGRESS_BUS_NAME, path=ingress_wire.INGRESS_OBJECT_PATH)

    def test_present_is_served_while_human_decision_is_in_flight(self) -> None:
        review_a = NONCE_MARK + "a" * (64 - len(NONCE_MARK))
        review_b = NONCE_MARK + "b" * (64 - len(NONCE_MARK))
        self.assertEqual(str(self.present(review_a)[0]), "presented")
        card_a = self.fake.last_card()
        _, tap = self.fake.callback(message=card_a, data=self.fake.button_data(card_a, 0))
        _wait(lambda: (self.approval_root / "held-1").exists(), what="HumanDecision held")
        started = time.monotonic()
        state, _ref = self.present(review_b)
        elapsed = time.monotonic() - started
        print(f"T5518-TIMING ingress-loop present={elapsed:.3f}s", file=sys.stderr)
        self.assertLess(elapsed, NONBLOCKING_BOUND_SECONDS)
        self.assertEqual(str(state), "presented")
        # the held decision was sent once, and is still in flight
        self.assertFalse((self.approval_root / "held-2").exists())
        # r1 N4: the tap was answered at tap time, while the decision is
        # still in flight (no verdict logged yet)
        self.wait_answered(tap)
        self.assertNotIn("event=decision_refused", self.log_text("ingress"))
        (self.approval_root / "release").write_text("go", encoding="utf-8")
        _wait(lambda: "detail=human_request_closed" in self.log_text("ingress"), what="refusal")
        self.assertFalse((self.approval_root / "held-2").exists())


class StallWithoutReplayBusTests(_AsyncBusMixin, _IngressBusCase):
    """Ask 5(2), over the bus: Present times out AFTER the ingress sent
    the card (sendMessage slower than the 1 s Present timeout), the human
    taps, and NOBODY replays RequestHuman. The approval service's own
    retry reaches 'presented' and the decision lands exactly once."""

    PRESENT_TIMEOUT_SECONDS = 1.0

    def test_present_lost_after_send_then_tap_completes_via_service_retry(self) -> None:
        review = self.approval_state["review_handle"]
        self.fake.send_delay_seconds = 2.0
        try:
            result = self.trigger_request(review, "stall-1")
            _wait(result.exists, what="first attempt settled")
            self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["state"], "pending")
            _wait(lambda: len(self.fake.messages) == 1, what="the late-sent card")
        finally:
            self.fake.send_delay_seconds = 0.0
        card = self.fake.last_card()
        _, tap = self.fake.callback(message=card, data=self.fake.button_data(card, 0))
        _wait(lambda: self.review_state(review)[0] == "granted", what="grant via retry")
        self.wait_answered(tap)
        state, summary = self.review_state(review)
        self.assertEqual(summary["authority_source"], "human")
        self.assertEqual(summary["human_request"]["state"], "decided")
        self.assertEqual(self.journal_rows("SELECT count(*) FROM provider_attestations")[0][0], 1)
        self.assertEqual(len(self.fake.messages), 1)  # the retry was a replay, never a 2nd card
        self.assertIn("event=present_replayed", self.log_text("ingress"))


if __name__ == "__main__":
    unittest.main()
