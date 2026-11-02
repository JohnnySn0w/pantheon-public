"""Fixture library + subprocess driver for the T-2920 U2 daemon-entrypoint
tests (``tools/test_host_control_daemon_entrypoints.py`` and
``tools/test_host_control_daemon_entrypoints_dbus.py``).

NOT a test module (no test cases; the ``tools/test_host_control_dbus_driver.py``
precedent). Two halves:

- Fixture library: ``broker_tree`` and ``approval_tree`` write a disposable
  root-config tree (``etc/config.json``, a trusted dir, a state dir) in the
  exact production schemas; ``seed_pending_human_requests`` journals pending
  human requests the way a running approval service would have before a
  restart.
- Driver entrypoint, one role per process::

      python3 -m tools.test_host_control_daemon_entrypoints_driver broker   <config.json>
      python3 -m tools.test_host_control_daemon_entrypoints_driver approval <config.json>
      python3 -m tools.test_host_control_daemon_entrypoints_driver ingress  <config.json>

  ``broker``/``approval`` call the REAL entrypoint ``run()`` with only the
  test seams swapped (the test-mode trusted loader for the caller's uid, and
  a private-bus connection factory). ``ingress`` is a recording stand-in for
  ``org.pantheon.HostApprovalTelegram1``: it records every ``Present`` and
  refuses it retryably, so the approval side keeps the requests pending.

Fixture boundaries, stated so evidence is never relabelled: one Unix uid
plays every role (real cross-uid is T-2924), the bus is a private permissive
dbus-daemon (gate 10: every refusal is in code), files are owned by the
caller's uid through ``RootOwnedFileLoader._open_for_test``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOST_OPERATION_FAMILIES = ["persona.install", "persona.verify"]
INGRESS_OFFSET = 3000
AGENT_OFFSET = 4000
OWNER_OFFSET = 1000
BROKER_OFFSET = 2000


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


#: what ``load_checkout_op_registry(<framework>)`` reads: the systems registry
#: and the in-repo component contracts. The T-5533 overlay symlinks them into
#: the checkout, which the component loader refuses ("resolves outside the
#: checkout"); a real root-owned framework copy holds them as files (U1's
#: closure), so the fixture makes them real copies too.
OP_REGISTRY_INPUTS = ("registry", "tools/germinate/components")


def _materialize_op_registry_inputs(framework: Path) -> None:
    import shutil

    from tools.test_host_control_dbus_driver import REPO_ROOT

    for relative in OP_REGISTRY_INPUTS:
        parts = Path(relative).parts
        current = framework
        for depth, part in enumerate(parts):
            current = current / part
            source = REPO_ROOT.joinpath(*parts[: depth + 1])
            if depth == len(parts) - 1:
                if current.is_symlink():
                    current.unlink()
                shutil.copytree(source, current, ignore=shutil.ignore_patterns("__pycache__"))
            elif current.is_symlink():
                current.unlink()
                current.mkdir(mode=0o700)
                for entry in sorted(source.iterdir()):
                    if entry.name not in ("__pycache__", parts[depth + 1]):
                        (current / entry.name).symlink_to(entry)


def broker_config_value(tree: dict, **overrides: object) -> dict:
    value = {
        "schema": "pantheon-host-broker-config/v1",
        "trusted_roots": [tree["trusted"]],
        "registry_path": tree["registry"],
        "policy_path": tree["policy"],
        "framework_root": tree["framework"],
        "journal_path": tree["journal"],
        "approval_uid": tree["approval_uid"],
        "monitor_uids": tree["monitor_uids"],
        "host_operation_families": list(HOST_OPERATION_FAMILIES),
        "plan_ttl_seconds": 3600,
        "broker_revision": "t2920-u2-test",
    }
    value.update(overrides)
    return value


def broker_tree(
    root: Path,
    *,
    subject_uid: int,
    approval_uid: int,
    monitor_uids: list[int],
    compensate: bool | None = None,
) -> dict:
    """A broker config tree: the T-2912 domain fixture (registry with three
    seats, policy, framework overlay, registered plans) under ``trusted``,
    the journal under ``state``, the config under ``etc``."""

    from tools.test_host_control_dbus_driver import build_domain

    _private_dir(root)
    trusted = root / "trusted"
    build_domain(trusted, subject_uid=subject_uid).close()
    _materialize_op_registry_inputs(trusted / "framework")
    state = _private_dir(root / "state")
    etc = _private_dir(root / "etc")
    tree = {
        "root": str(root),
        "trusted": str(trusted),
        "registry": str(trusted / "registry.json"),
        "policy": str(trusted / "policy.json"),
        "framework": str(trusted / "framework"),
        "journal": str(state / "broker.sqlite3"),
        "approval_uid": approval_uid,
        "monitor_uids": list(monitor_uids),
        "config_path": str(etc / "config.json"),
        "subject_uid": subject_uid,
    }
    overrides = {} if compensate is None else {"compensate": compensate}
    _write_json(etc / "config.json", broker_config_value(tree, **overrides))
    return tree


def approval_policy_value(caller_uid: int) -> dict:
    from tools.test_host_control_telegram_ingress_fixtures import human_policy_raw

    return human_policy_raw(
        ingress_uid=caller_uid + INGRESS_OFFSET,
        agent_uid=caller_uid + AGENT_OFFSET,
        owner_uid=caller_uid + OWNER_OFFSET,
    )


def approval_config_value(tree: dict, **overrides: object) -> dict:
    from tools.test_host_control_telegram_ingress_fixtures import HUMAN_CHAT, HUMAN_USER

    value = {
        "schema": "pantheon-host-approval-config/v1",
        "trusted_roots": [tree["trusted"]],
        "approver_policy_path": tree["policy"],
        "journal_path": tree["journal"],
        "broker_uid": tree["broker_uid"],
        "monitor_uids": tree["monitor_uids"],
        "human_ingress": {
            "ingress_uid": tree["ingress_uid"],
            "human_subject": "operator",
            "provider_user_id": str(HUMAN_USER),
            "provider_chat_id": str(HUMAN_CHAT),
        },
    }
    value.update(overrides)
    return value


def approval_tree(root: Path, *, caller_uid: int, policy: dict | None = None) -> dict:
    _private_dir(root)
    trusted = _private_dir(root / "trusted")
    state = _private_dir(root / "state")
    etc = _private_dir(root / "etc")
    policy_path = trusted / "approver-policy.json"
    _write_json(policy_path, policy or approval_policy_value(caller_uid))
    tree = {
        "root": str(root),
        "trusted": str(trusted),
        "policy": str(policy_path),
        "journal": str(state / "approval.sqlite3"),
        "broker_uid": caller_uid + BROKER_OFFSET,
        "ingress_uid": caller_uid + INGRESS_OFFSET,
        "monitor_uids": [caller_uid],
        "config_path": str(etc / "config.json"),
    }
    _write_json(etc / "config.json", approval_config_value(tree))
    return tree


class _RecordingOnlyPresenter:
    """Async presenter that never replies: ``request_human`` only journals."""

    def begin_present(self, *args, on_done) -> None:  # pragma: no cover - never armed here
        del args, on_done


class _NoTimers:
    def call_later(self, delay_seconds, callback) -> None:  # pragma: no cover
        del delay_seconds, callback


def seed_pending_human_requests(tree: dict, count: int) -> list[str]:
    """Journal ``count`` pending human requests (as a running service would
    have before a restart); returns their REVIEW handles in creation order."""

    from tools.host_control.approval.journal import ApprovalJournal
    from tools.host_control.approval.policy import PolicyApproverAuthorizer, parse_approver_policy
    from tools.host_control.approval.service import ApprovalService
    from tools.test_host_control_approval_dbus_driver import FakeApprovalMetadataSource
    from tools.test_host_control_telegram_ingress_fixtures import human_plan, registration

    caller = int(tree["monitor_uids"][0])
    metadata = FakeApprovalMetadataSource()
    journal = ApprovalJournal.open(Path(tree["journal"]))
    reviews: list[str] = []
    try:
        service = ApprovalService(
            journal=journal,
            metadata_source=metadata,
            clock=lambda: datetime.now(timezone.utc),
            authorizer=PolicyApproverAuthorizer(parse_approver_policy(approval_policy_value(caller))),
            registered_consumer_uids=frozenset((int(tree["broker_uid"]),)),
            human_ingress=registration(int(tree["ingress_uid"])),
            card_presenter=_RecordingOnlyPresenter(),
            delivery_scheduler=_NoTimers(),
            human_card_ttl=timedelta(minutes=15),
        )
        for index in range(count):
            plan_handle = format(0x7000 + index, "064x")
            digest = format(0x9000 + index, "064x")
            metadata.plans[plan_handle] = human_plan(digest)
            metadata.summaries[plan_handle] = '{"effects":["seeded request %d"]}' % index
            review = service.review("specialist", plan_handle, digest, None).record
            result = service.request_human("specialist", review.review_handle, digest, f"seed-{index}")
            assert result.state == "pending", result.state
            reviews.append(review.review_handle)
    finally:
        journal.close()
    return reviews


# ---------------------------------------------------------------------------
# driver half
# ---------------------------------------------------------------------------


def _test_loader(roots):
    from tools.host_control.trusted_files import RootOwnedFileLoader

    return RootOwnedFileLoader._open_for_test(tuple(roots), expected_owner_uid=os.getuid())


def _private_connect(address: str):
    def connect(main_loop: bool):
        import dbus.bus
        import dbus.mainloop

        if main_loop:
            return dbus.bus.BusConnection(address)
        return dbus.bus.BusConnection(address, mainloop=dbus.mainloop.NULL_MAIN_LOOP)

    return connect


def serve_ingress(config: dict) -> int:
    import dbus
    import dbus.bus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    from tools.host_control.approval import wire

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    record_path = Path(config["present_log"])
    connection = dbus.bus.BusConnection(config["bus_address"])

    class RecordingIngress(dbus.service.Object):
        @dbus.service.method(wire.TELEGRAM_INGRESS_INTERFACE, in_signature="sssss", out_signature="ss")
        def Present(self, review_handle, expected_digest, summary_json, callback_nonce, expires_at):
            with record_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"review_handle": str(review_handle)}) + "\n")
            raise dbus.exceptions.DBusException(
                "recording stand-in", name=f"{wire.TELEGRAM_INGRESS_ERROR_NAMESPACE}.Unavailable"
            )

    bus_name = dbus.service.BusName(wire.TELEGRAM_INGRESS_BUS_NAME, connection)
    obj = RecordingIngress(connection, wire.TELEGRAM_INGRESS_OBJECT_PATH)
    _keepalive = (bus_name, obj)
    GLib.MainLoop().run()
    del _keepalive
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("broker", "approval", "ingress"):
        print("usage: test_host_control_daemon_entrypoints_driver broker|approval|ingress <config.json>")
        return 2
    config = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    if argv[1] == "ingress":
        return serve_ingress(config)
    connect = _private_connect(config["bus_address"])
    if argv[1] == "broker":
        from tools.host_control.__main__ import run as broker_run

        return broker_run(config_path=Path(config["config_path"]), open_loader=_test_loader, connect=connect)
    from tools.host_control.approval.__main__ import run as approval_run

    return approval_run(
        config_path=Path(config["config_path"]),
        open_loader=_test_loader,
        connect=connect,
        resume_page=int(config.get("resume_page", 100)),
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
