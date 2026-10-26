"""Subprocess service host for the T-2919 private-bus tests.

NOT a test module (zero test cases; the
``tools/test_host_control_approval_dbus_driver.py`` precedent).

``python3 -m tools.test_host_control_telegram_ingress_driver <config.json>``

Two roles, chosen by the config:

- ``approval``: a real approval journal + service + policy authorizer
  with a ``HumanIngressRegistration``, the real non-blocking
  ``DbusTelegramPresenter`` + ``GLibScheduler`` (T-5518: the service
  retries an undelivered card itself; on a separate client connection),
  and the real ``HostApproval1Object``. Owns ``org.pantheon.HostApproval1``.
- ``held_approval``: a STUB that owns ``org.pantheon.HostApproval1`` and
  holds every ``HumanDecision`` reply until a ``release`` file appears
  (then refuses it ``human_request_closed``) — proves the ingress loop
  keeps serving ``Present`` while a decision is in flight (T-5518).
- ``ingress``: the real ingress stack built by ``main.build_core`` —
  trusted config load, ORG-051 lease over a fixture token file, getMe —
  pointed at the in-test-process loopback fake Bot API, then
  ``run_ingress`` (owns ``org.pantheon.HostApprovalTelegram1``).

Uids are scenario data relative to the caller's real uid (one test
process has one real uid). Inputs arrive in a JSON config FILE: argv
carries only its path (the leak tests read /proc/<pid>/cmdline).
"""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _approval(config: dict) -> int:
    import dbus
    import dbus.bus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    from tools.host_control.approval import wire
    from tools.host_control.approval.dbus_service import HostApproval1Object
    from tools.host_control.approval.human import HumanIngressRegistration
    from tools.host_control.approval.identity import ApproverResolver
    from tools.host_control.approval.journal import ApprovalJournal
    from tools.host_control.approval.policy import (
        PolicyApproverAuthorizer,
        parse_approver_policy,
    )
    from tools.host_control.approval.service import ApprovalService
    from tools.host_control.approval.telegram_presenter import (
        DbusTelegramPresenter,
        GLibScheduler,
    )
    from tools.test_host_control_telegram_ingress_fixtures import (
        CANARY_DIGEST,
        EFFECT_CANARY,
        HUMAN_CHAT,
        HUMAN_USER,
        OTHER_DIGEST,
        OTHER_PLAN_HANDLE,
        PLAN_HANDLE,
        FakeApprovalMetadataSource,
        MarkedHandles,
        human_plan,
        human_policy_raw,
    )

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    caller = os.getuid()
    ingress_uid = caller + int(config["ingress_offset"])
    # the agent approver's uid is scenario data: it must NOT equal the
    # ingress uid (the service refuses that, review finding 7), so when the
    # ingress runs as the caller the approver lives on a shifted uid and
    # the driver acts as it in-process (review + file-triggered requests)
    agent_uid = caller + int(config.get("agent_offset", 0))
    policy_raw = human_policy_raw(
        ingress_uid=ingress_uid,
        agent_uid=agent_uid,
        owner_uid=caller + 1000,
    )
    root = Path(config["root"])
    root.mkdir(parents=True, exist_ok=True)
    metadata = FakeApprovalMetadataSource()
    metadata.plans[PLAN_HANDLE] = human_plan()
    metadata.summaries[PLAN_HANDLE] = (
        '{"effects":["create account svc-demo"],"marker":"' + EFFECT_CANARY + '"}'
    )
    metadata.plans[OTHER_PLAN_HANDLE] = human_plan(OTHER_DIGEST)
    journal = ApprovalJournal.open(root / "approval.sqlite3")
    client_connection = dbus.bus.BusConnection(config["bus_address"])
    service = ApprovalService(
        journal=journal,
        metadata_source=metadata,
        clock=lambda: datetime.now(timezone.utc),
        authorizer=PolicyApproverAuthorizer(parse_approver_policy(policy_raw)),
        handle_factory=MarkedHandles(),
        registered_consumer_uids=frozenset((caller + 2000,)),
        human_ingress=HumanIngressRegistration(
            uid=ingress_uid,
            provider="telegram",
            human_subject="operator",
            provider_user_id=str(HUMAN_USER),
            provider_chat_id=str(HUMAN_CHAT),
        ),
        card_presenter=DbusTelegramPresenter(
            lambda: client_connection,
            timeout_seconds=float(config.get("present_timeout_seconds", 10)),
        ),
        delivery_scheduler=GLibScheduler(),
        human_card_ttl=timedelta(minutes=15),
    )
    service.resume_human_deliveries()
    connection = dbus.bus.BusConnection(config["bus_address"])
    bus_name = dbus.service.BusName(wire.BUS_NAME, connection)
    obj = HostApproval1Object(
        connection,
        service=service,
        approver_resolver=ApproverResolver(parse_approver_policy(policy_raw)),
        consumer_uid=caller + 2000,
        ingress_uid=ingress_uid,
    )
    state = {"plan_handle": PLAN_HANDLE, "plan_digest": CANARY_DIGEST}
    if config.get("driver_review"):
        # T-5518: concurrency scenarios need more than one open review
        # (a second review of the SAME plan would replay the first one, so
        # the second opens the other fixture plan)
        plans = ((PLAN_HANDLE, CANARY_DIGEST), (OTHER_PLAN_HANDLE, OTHER_DIGEST))
        count = int(config.get("driver_review_count", 1))
        state["review_handles"] = []
        state["review_digests"] = []
        for plan_handle, digest in plans[:count]:
            state["review_handles"].append(
                service.review("specialist", plan_handle, digest, None).record.review_handle
            )
            state["review_digests"].append(digest)
        state["review_handle"] = state["review_handles"][0]
    (root / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def poll_triggers() -> bool:
        # driver-side RequestHuman as 'specialist' (the approver's uid is not
        # the test process's), triggered by files the test writes
        for trigger in sorted(root.glob("trigger-*.json")):
            spec = json.loads(trigger.read_text(encoding="utf-8"))
            trigger.rename(trigger.with_suffix(".taken"))
            out = root / trigger.name.replace("trigger-", "result-")
            try:
                result = service.request_human(
                    "specialist", spec["review_handle"], spec["digest"], spec["key"]
                )
            except Exception as exc:  # report, never crash the loop
                _write_result(out, {"error": str(exc)[:64]})
                continue
            if result.state == "pending":
                # the same deferred settle the D-Bus RequestHuman uses

                def settled(outcome, error, out=out) -> None:
                    _write_result(out, {"error": type(error).__name__} if outcome is None
                                  else {"request_handle": outcome[0], "state": outcome[1]})

                if service.deliver_human_request(result.request_handle, settled):
                    continue
            _write_result(out, {"request_handle": result.request_handle, "state": result.state})
        return True

    GLib.timeout_add(50, poll_triggers)
    keep = (bus_name, obj, client_connection)
    GLib.MainLoop().run()
    del keep
    return 0


def _write_result(out: Path, body: dict) -> None:
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(body), encoding="utf-8")
    tmp.rename(out)


def _held_approval(config: dict) -> int:
    import dbus
    import dbus.bus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    from tools.host_control.approval import wire

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    root = Path(config["root"])
    root.mkdir(parents=True, exist_ok=True)
    connection = dbus.bus.BusConnection(config["bus_address"])
    held: list = []

    class Held(dbus.service.Object):
        @dbus.service.method(
            wire.HUMAN_INGRESS_INTERFACE,
            in_signature="ssssss",
            out_signature="ss",
            async_callbacks=("reply_cb", "error_cb"),
        )
        def HumanDecision(self, review_handle, expected_digest, decision,
                          source_event_handle, callback_nonce, idempotency_key,
                          reply_cb=None, error_cb=None) -> None:
            del review_handle, expected_digest, decision, source_event_handle
            del callback_nonce, idempotency_key, reply_cb
            held.append(error_cb)
            (root / f"held-{len(held)}").write_text("held", encoding="utf-8")

    obj = Held(connection, wire.OBJECT_PATH)
    bus_name = dbus.service.BusName(wire.BUS_NAME, connection)

    def poll_release() -> bool:
        if (root / "release").exists():
            while held:
                held.pop(0)(dbus.exceptions.DBusException(
                    "human_request_closed", name=wire.ERROR_NAMESPACE + ".Failed"))
        return True

    GLib.timeout_add(50, poll_release)
    (root / "held-ready").write_text("ready", encoding="utf-8")
    keep = (obj, bus_name)
    GLib.MainLoop().run()
    del keep
    return 0


def _ingress(config: dict) -> int:
    import dbus
    import dbus.bus
    import dbus.mainloop.glib

    from tools.host_control.telegram_ingress.config import load_ingress_config
    from tools.host_control.telegram_ingress.core import IngressLog
    from tools.host_control.telegram_ingress.dbus_service import (
        DbusHumanDecider,
        run_ingress,
    )
    from tools.host_control.telegram_ingress.main import build_core

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    ingress_config = load_ingress_config(
        Path(config["ingress_config"]), expected_owner_uid=os.getuid()
    )
    connection = dbus.bus.BusConnection(config["bus_address"])
    decider = DbusHumanDecider(lambda: connection, timeout_seconds=10)
    log = IngressLog(sys.stderr)
    core = build_core(
        ingress_config,
        decider=decider,
        log=log,
        api_base=config["api_base"],
    )
    ready = Path(config["root"]) / "ingress-ready"

    def on_ready() -> None:
        ready.write_text("ready", encoding="utf-8")

    # one test process has one real uid, so the config's (dedicated,
    # distinct) approval uid is a placeholder and the Present caller uid
    # is scenario data relative to the caller; production main() passes
    # config.approval_uid
    return run_ingress(
        core,
        connection,
        approval_uid=os.getuid() + int(config["present_caller_offset"]),
        poll_timeout_seconds=ingress_config.poll_timeout_seconds,
        poll_interval_ms=100,
        on_ready=on_ready,
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    config = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    if config["role"] == "approval":
        return _approval(config)
    if config["role"] == "ingress":
        return _ingress(config)
    if config["role"] == "held_approval":
        return _held_approval(config)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
