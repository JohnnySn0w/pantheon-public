"""Subprocess service host + shared fixture library for the T-2913
private-bus integration tests (``org.pantheon.HostApproval1``).

NOT a test module: it contains zero unittest test cases or functions;
pytest collection here is deliberately a no-op. It is named test_* so
it sits unambiguously beside its consumer,
``tools/test_host_control_approval_dbus.py`` (the T-2912 OQ8-precedent
naming).

Two halves:

- Fixture library (no dbus imports): the spying
  ``FakeApprovalMetadataSource``, approver-policy document builders,
  and ``build_approval_domain`` — a real ``ApprovalJournal`` +
  ``ApprovalService`` + ``PolicyApproverAuthorizer`` stack in a
  caller-provided root. The test module imports these for its
  unguarded and in-process tests, so there is exactly one fixture
  definition (single-source over duplicate-and-drift).
- Driver entrypoint: ``python3 -m tools.test_host_control_approval_dbus_driver
  <config.json>`` connects to the private bus address in the config,
  builds the SCENARIO named there (which uid class equals the caller's
  real uid is scenario data — the config-relative-uid trick from the
  T-2912 harness: one test process has one real uid, so the service's
  configured uid classes shift relative to it), exports the
  ``HostApproval1Object``, writes a state file with the seeded
  handles, and serves a GLib main loop until killed. All stdout/stderr
  is redirected to files by the spawning test.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from tools.host_control.approval.contracts import PlanMetadata
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.policy import (
    PolicyApproverAuthorizer,
    parse_approver_policy,
)
from tools.host_control.approval.service import ApprovalService
from tools.host_control.contracts import UnknownHandle

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fixed fixture material (64-hex handles/digests).
PLAN_HANDLE = "2" * 64
PLAN_DIGEST = "3" * 64
PLAN_B_HANDLE = "4" * 64
PLAN_B_DIGEST = "5" * 64
EFFECT_DIGEST = "f" * 64


class FakeApprovalMetadataSource:
    """Programmable ``PlanMetadataSource`` seam; records exact calls.

    ``events`` is the ORDERED spy log of (method, plan_handle) — the
    gate-12 disclosure proofs assert call COUNT and ORDER against it,
    not merely withheld return values (tripwire-over-vacuity)."""

    def __init__(self) -> None:
        self.plans: dict[str, PlanMetadata] = {}
        self.summaries: dict[str, str] = {}
        self.error: Exception | None = None
        self.events: list[tuple[str, str]] = []

    def plan_metadata(self, plan_handle: str) -> PlanMetadata:
        self.events.append(("plan_metadata", plan_handle))
        if self.error is not None:
            raise self.error
        if plan_handle not in self.plans:
            raise UnknownHandle("unknown plan handle")
        return self.plans[plan_handle]

    def plan_summary(
        self,
        plan_handle: str,
        expected_digest: str,
    ) -> tuple[str, str]:
        self.events.append(("plan_summary", plan_handle))
        if self.error is not None:
            raise self.error
        if plan_handle not in self.plans:
            raise UnknownHandle("unknown plan handle")
        metadata = self.plans[plan_handle]
        summary = self.summaries.get(
            plan_handle, f"redacted-effects:{plan_handle[:8]}"
        )
        return metadata.plan_digest, summary


def approver_entry(
    families: tuple[str, ...] = ("persona.install",),
    seat_classes: tuple[str, ...] = ("worker-seat",),
    max_ttl_seconds: int = 3600,
) -> dict:
    """One approver roster entry (agent decision source)."""

    return {
        "decision_source": "agent",
        "grants": [
            {
                "family": family,
                "seat_classes": list(seat_classes),
                "max_ttl_seconds": max_ttl_seconds,
            }
            for family in families
        ],
    }


def approver_policy_raw(
    *,
    subjects: dict[str, int],
    approvers: dict[str, dict],
    seat_classes: dict[str, str] | None = None,
    family_categories: dict[str, str] | None = None,
    revokers: list[str] | None = None,
    revision: str = "approver-policy-r1",
) -> dict:
    """Minimal valid ``pantheon-approval-policy/v1`` document."""

    return {
        "schema": "pantheon-approval-policy/v1",
        "revision": revision,
        "seat_classes": seat_classes or {"recovery": "worker-seat"},
        "subjects": subjects,
        "family_categories": (
            family_categories or {"persona.install": "seat_apply_reversible"}
        ),
        "approvers": approvers,
        "revokers": revokers or [],
    }


def plan_metadata_fixture(
    *,
    owner: str,
    plan_digest: str = PLAN_DIGEST,
) -> PlanMetadata:
    """A broker-plan metadata view owned by ``owner`` (performer ==
    owner: the broker-plan convention, per the landed approval_source
    docstring)."""

    return PlanMetadata(
        owner=owner,
        performer=owner,
        target_seat_id="recovery",
        operation_families=("persona.install",),
        plan_digest=plan_digest,
        effect_digest=EFFECT_DIGEST,
        manifest_revision="manifest-r1",
        policy_revision="policy-r1",
    )


@dataclass
class ApprovalDomainFixture:
    root: Path
    journal: ApprovalJournal
    metadata: FakeApprovalMetadataSource
    service: ApprovalService
    policy_raw: dict

    def close(self) -> None:
        self.journal.close()


def build_approval_domain(
    root: Path,
    *,
    policy_raw: dict,
    consumer_uids: frozenset[int] = frozenset(),
    metadata: FakeApprovalMetadataSource | None = None,
    clock: Callable[[], datetime] | None = None,
    review_ttl: timedelta = timedelta(hours=1),
    grant_ttl: timedelta = timedelta(hours=1),
) -> ApprovalDomainFixture:
    """A real approval domain stack over the fake metadata seam."""

    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    metadata = metadata or FakeApprovalMetadataSource()
    journal = ApprovalJournal.open(root / "approval.sqlite3")
    service = ApprovalService(
        journal=journal,
        metadata_source=metadata,
        clock=clock or (lambda: datetime.now(timezone.utc)),
        authorizer=PolicyApproverAuthorizer(
            parse_approver_policy(policy_raw)
        ),
        registered_consumer_uids=consumer_uids,
        review_ttl=review_ttl,
        grant_ttl=grant_ttl,
    )
    return ApprovalDomainFixture(
        root=root,
        journal=journal,
        metadata=metadata,
        service=service,
        policy_raw=policy_raw,
    )


# ---------------------------------------------------------------------------
# Scenarios (driver half) — which uid class equals the caller's real uid
# is scenario data; every other uid is offset from it. Each returns
# (policy_raw, consumer_uid, ingress_uid, seed) where seed is a callable
# run against the built fixture to produce the state dict.
# ---------------------------------------------------------------------------


def _seed_plans(fixture: ApprovalDomainFixture, owner: str) -> None:
    fixture.metadata.plans[PLAN_HANDLE] = plan_metadata_fixture(owner=owner)
    fixture.metadata.plans[PLAN_B_HANDLE] = plan_metadata_fixture(
        owner=owner, plan_digest=PLAN_B_DIGEST
    )


def _scenario_reviewer(caller_uid: int) -> dict:
    """Caller IS the registered approver 'specialist'; plan owners live on a
    DISTINCT uid — the happy reviewer path."""

    return {
        "policy": approver_policy_raw(
            subjects={"specialist": caller_uid, "alpha": caller_uid + 1000},
            approvers={"specialist": approver_entry()},
        ),
        "plan_owner": "alpha",
        "consumer_offset": 2000,
        "ingress_offset": 3000,
    }


def _scenario_requester_only(caller_uid: int) -> dict:
    """Caller's uid is a registered SUBJECT with no approver standing —
    the literal requester-only uid (gate 5, server half). The approver
    roster exists but lives on a different uid."""

    return {
        "policy": approver_policy_raw(
            subjects={"alpha": caller_uid, "specialist": caller_uid + 2000},
            approvers={"specialist": approver_entry()},
        ),
        "plan_owner": "alpha",
        "consumer_offset": 2000,
        "ingress_offset": 3000,
    }


def _scenario_self_owner(caller_uid: int) -> dict:
    """Caller resolves to approver 'alpha' who IS the plan owner —
    gate 11 arm (i): approver_is_plan_owner."""

    return {
        "policy": approver_policy_raw(
            subjects={"alpha": caller_uid},
            approvers={"alpha": approver_entry()},
        ),
        "plan_owner": "alpha",
        "consumer_offset": 2000,
        "ingress_offset": 3000,
    }


def _scenario_self_trust_domain(caller_uid: int) -> dict:
    """Caller resolves to approver 'specialist'; the plan owner 'alpha' is a
    DISTINCT subject mapped to the SAME uid — gate 11 arm (ii):
    approver_shares_trust_domain. 'mason' is a legitimate approver on a
    third uid whose driver-side review-open lets the bus caller attempt
    the DECIDE arm too."""

    return {
        "policy": approver_policy_raw(
            subjects={
                "specialist": caller_uid,
                "alpha": caller_uid,
                "mason": caller_uid + 3000,
            },
            approvers={
                "specialist": approver_entry(),
                "mason": approver_entry(),
            },
        ),
        "plan_owner": "alpha",
        "consumer_offset": 2000,
        "ingress_offset": 3000,
        "open_review_as": "mason",
    }


def _scenario_consumer(caller_uid: int) -> dict:
    """Caller IS the configured consumer (broker) uid; a granted grant
    is seeded driver-side by approver 'mason' (distinct uid)."""

    return {
        "policy": approver_policy_raw(
            subjects={"alpha": caller_uid + 1000, "mason": caller_uid + 3000},
            approvers={"mason": approver_entry()},
        ),
        "plan_owner": "alpha",
        "consumer_offset": 0,
        "ingress_offset": 3000,
        "seed_grant_as": "mason",
    }


def _scenario_ingress(caller_uid: int) -> dict:
    """Caller IS the configured ingress uid."""

    return {
        "policy": approver_policy_raw(
            subjects={"alpha": caller_uid + 1000, "specialist": caller_uid + 2000},
            approvers={"specialist": approver_entry()},
        ),
        "plan_owner": "alpha",
        "consumer_offset": 2000,
        "ingress_offset": 0,
    }


def _scenario_cross_service(caller_uid: int) -> dict:
    """Metadata comes from the LIVE broker service on the same private
    bus through the real ``BrokerPlanMetadataClient`` (OQ5). The broker
    driver's seat 'alpha' runs as the caller's uid and seat 'bravo' as
    60001, so: specialist reviewing bravo's plan is the authorized
    cross-service path, and specialist reviewing alpha's plan is the gate-11
    trust-domain denial proven END-TO-END across two real services."""

    return {
        "policy": approver_policy_raw(
            subjects={
                "specialist": caller_uid,
                "alpha": caller_uid,
                "bravo": 60001,
            },
            approvers={"specialist": approver_entry()},
        ),
        "plan_owner": None,
        "consumer_offset": 2000,
        "ingress_offset": 3000,
        "metadata_from_broker": True,
    }


def _scenario_cross_service_requester(caller_uid: int) -> dict:
    """T-2916 (gate 5, requester-only seat end to end): the same two real
    services as ``cross_service``, but the caller's uid resolves only to
    the plan-owning subject 'alpha'; the approver roster ('specialist') lives
    on a different uid. Every Reviewer call from the caller - through the
    approver MCP adapter or directly over D-Bus - must be refused."""

    return {
        "policy": approver_policy_raw(
            subjects={
                "specialist": caller_uid + 2000,
                "alpha": caller_uid,
                "bravo": 60001,
            },
            approvers={"specialist": approver_entry()},
        ),
        "plan_owner": None,
        "consumer_offset": 2000,
        "ingress_offset": 3000,
        "metadata_from_broker": True,
    }


def _scenario_cross_service_revoked(caller_uid: int) -> dict:
    """T-2916 (ORG-052 §7 revocation): ``cross_service`` after a policy
    revision that removes 'specialist' (the caller's uid) from the approver
    roster. 'specialist' stays a known subject; 'mason' is the remaining
    approver on another uid. Run against the SAME journal root as a prior
    ``cross_service`` run, it models a policy install + service restart
    while the approver seat's harness config is stale."""

    return {
        "policy": approver_policy_raw(
            subjects={
                "specialist": caller_uid,
                "alpha": caller_uid,
                "bravo": 60001,
                "mason": caller_uid + 3000,
            },
            approvers={"mason": approver_entry()},
            revision="approver-policy-r2",
        ),
        "plan_owner": None,
        "consumer_offset": 2000,
        "ingress_offset": 3000,
        "metadata_from_broker": True,
    }


SCENARIOS: dict[str, Callable[[int], dict]] = {
    "cross_service_requester": _scenario_cross_service_requester,
    "cross_service_revoked": _scenario_cross_service_revoked,
    "reviewer": _scenario_reviewer,
    "requester_only": _scenario_requester_only,
    "self_owner": _scenario_self_owner,
    "self_trust_domain": _scenario_self_trust_domain,
    "consumer": _scenario_consumer,
    "ingress": _scenario_ingress,
    "cross_service": _scenario_cross_service,
}


def serve(config_path: str) -> int:
    """Driver entrypoint: export the approval service on the private
    bus named in the config and run until killed."""

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    import os

    import dbus
    import dbus.bus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    from tools.host_control.approval import wire
    from tools.host_control.approval.dbus_service import HostApproval1Object
    from tools.host_control.approval.identity import ApproverResolver
    from tools.host_control.approval.policy import parse_approver_policy

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    caller_uid = os.getuid()
    scenario = SCENARIOS[config["scenario"]](caller_uid)
    consumer_uid = caller_uid + scenario["consumer_offset"]
    ingress_uid = caller_uid + scenario["ingress_offset"]
    root = Path(config["root"])
    metadata = None
    if scenario.get("metadata_from_broker"):
        from tools.host_control.approval.broker_client import (
            BrokerPlanMetadataClient,
        )

        # a SEPARATE client connection: the service handler makes
        # blocking broker calls while inside the GLib dispatch, so the
        # client never shares the service's socket (no re-entrancy)
        client_connection = dbus.bus.BusConnection(config["bus_address"])
        metadata = BrokerPlanMetadataClient(client_connection)
    fixture = build_approval_domain(
        root,
        policy_raw=scenario["policy"],
        consumer_uids=frozenset({consumer_uid}),
        metadata=metadata,
    )
    if scenario["plan_owner"] is not None:
        _seed_plans(fixture, scenario["plan_owner"])
    state: dict = {
        "plan_handle": PLAN_HANDLE,
        "plan_digest": PLAN_DIGEST,
        "plan_b_handle": PLAN_B_HANDLE,
        "plan_b_digest": PLAN_B_DIGEST,
    }
    if "open_review_as" in scenario:
        opened = fixture.service.review(
            scenario["open_review_as"],
            PLAN_HANDLE,
            PLAN_DIGEST,
            None,
        )
        state["open_review_handle"] = opened.record.review_handle
    if "seed_grant_as" in scenario:
        approver = scenario["seed_grant_as"]
        opened = fixture.service.review(
            approver, PLAN_HANDLE, PLAN_DIGEST, None
        )
        decided = fixture.service.decide(
            approver,
            opened.record.review_handle,
            PLAN_DIGEST,
            "granted",
            "approved",
            "ticket:T-2913-seed",
            "seed-decide-1",
        )
        state["approval_handle"] = decided.approval_handle
        # a second granted grant, on a distinct plan (PLAN_B): the
        # digest-mismatch arm asserts ValidateAndBind(approval_b_handle,
        # plan_b_handle, plan_digest) denies digest_mismatch; the
        # cross-grant arm asserts ValidateAndBind(approval_handle,
        # plan_b_handle, plan_b_digest) denies plan_mismatch
        opened_b = fixture.service.review(
            approver, PLAN_B_HANDLE, PLAN_B_DIGEST, None
        )
        decided_b = fixture.service.decide(
            approver,
            opened_b.record.review_handle,
            PLAN_B_DIGEST,
            "granted",
            "approved",
            "ticket:T-2913-seed",
            "seed-decide-2",
        )
        state["approval_b_handle"] = decided_b.approval_handle
    connection = dbus.bus.BusConnection(config["bus_address"])
    bus_name = dbus.service.BusName(wire.BUS_NAME, connection)
    service_object = HostApproval1Object(
        connection,
        service=fixture.service,
        approver_resolver=ApproverResolver(
            parse_approver_policy(scenario["policy"])
        ),
        consumer_uid=consumer_uid,
        ingress_uid=ingress_uid,
    )
    state_path = root / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    # keep the name and object alive for the life of the loop
    _keepalive = (bus_name, service_object)
    GLib.MainLoop().run()
    del _keepalive
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: test_host_control_approval_dbus_driver <config.json>")
        return 2
    return serve(argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
