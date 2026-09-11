"""The ``org.pantheon.HostApproval1`` system D-Bus service (ORG-052
Unit 3b, T-2913).

Thin transport, zero domain logic (the T-2912 N1 shape): every handler
resolves the live bus sender to a Unix uid, re-authorizes at the
transport boundary FOR ITS INTERFACE, calls exactly one landed domain
method, and maps the result or exception onto the closed wire contract
in ``tools.host_control.approval.wire``. No policy decisions, no state
machines, no journal access live here.

Identity is never a caller argument (ORG-052 §2): the subject comes
only from ``connection.get_unix_user(sender)``. Per-interface gates,
each BEFORE any domain call, each refusing as the uniform
``PolicyDenied("subject_denied")`` authorization verdict:

- ``Reviewer`` — the uid must resolve to exactly one approver-roster
  subject via the root-owned approver policy
  (``tools.host_control.approval.identity``). The RESOLVED subject —
  never a caller string — feeds the domain, where the landed T-2911
  separation-of-duty rules (owner/performer/uid-trust-domain/chain)
  become live against real bus identity. That composition is gate 11:
  a second process under a registered uid presents the same uid, so it
  resolves to the same subject and the same trust domain, and can never
  approve that uid's own plan.
- ``Consumer`` — the uid must equal the configured broker uid; the
  domain's ``registered_consumer_uids`` allowlist then rechecks the
  same live uid (defense in depth, deliberately duplicated).
- ``HumanIngress`` — the uid must equal the configured ingress uid.

Bus policy (staged under ``dist/``) is defence in depth and never the
perimeter: these refusals hold on a fully permissive bus (gate 10
posture).

``RequestHuman`` and ``HumanDecision`` are exposed-but-refusing (the
broker ``Verify`` precedent): they MUST introspect as part of the
locked inventory (gate 2), the identity gate fires FIRST so an
unauthorized caller gets the authorization refusal rather than an
Unsupported error, and the authorized caller gets the bounded
``Unsupported`` error naming T-2919 — the human-request and
provider-authenticated decision paths are that unit's (the journal's
``provider_attestations`` table exists with no writer, and the domain
``decide`` mints only ``authority_source='agent'``).

DEVIATION RECORD (T-2913 OQ6, PO-acked): the broker-side D-Bus client
of ``HostApproval1.Consumer`` (the real ``ApprovalStatusSource`` /
``ApprovalBinder``) does NOT land in this unit — it is deferred to
T-2917, whose authorize/bind leg is its real consumer. The landed
broker ``dbus_service.py`` docstring's "lands in T-2913" forward
reference is superseded by this note; the broker's shipped fail-closed
``UnavailableApprovalStatus`` default remains correct until T-2917
wires the client.

The dbus imports are guarded so a runner without dbus-python imports
this module safely; ``DBUS_AVAILABLE`` is the flag. On the canonical
host dbus-python IS present under system python3 — a skip there means
the guard broke, not that the environment is missing.

No ``main()`` in this unit: the entrypoint, its root-owned config
schema, and the approver-policy file wiring are the T-2920 install
leg's (the T-2912 OQ6-disposition shape). Nothing here connects to the
real system bus; tests use private buses only.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Protocol

from tools.host_control.approval import wire
from tools.host_control.approval.service import ApprovalService
from tools.host_control.contracts import PolicyDenied

try:  # pragma: no cover - exercised implicitly on every import
    import dbus
    import dbus.service

    DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover - dbus-less runners only
    DBUS_AVAILABLE = False


class ApproverSubjectResolver(Protocol):
    """Seam for the Reviewer identity gate.

    The real implementation is
    ``tools.host_control.approval.identity.ApproverResolver`` over the
    parsed root-owned approver policy; fakes supply it in tests. The
    input is ONLY the bus-attested uid; ``None`` means refuse (both the
    unregistered and the ambiguous case — indistinguishable by design).
    """

    def subject_for_uid(self, uid: int) -> str | None: ...


_UNSUPPORTED_HUMAN_REQUEST = (
    "human approval requests are unsupported in this revision; "
    "the RequestHuman path lands with T-2919"
)
_UNSUPPORTED_HUMAN_DECISION = (
    "provider-authenticated human decisions are unsupported in this "
    "revision; the HumanDecision path lands with T-2919"
)


if DBUS_AVAILABLE:

    def _wire_error(exc: BaseException) -> "dbus.exceptions.DBusException":
        """Map any exception onto the closed, bounded wire contract."""

        return dbus.exceptions.DBusException(
            wire.error_body_for(exc),
            name=wire.error_name_for(exc),
        )

    def _named_error(name: str, body: str) -> "dbus.exceptions.DBusException":
        return dbus.exceptions.DBusException(body, name=name)

    class HostApproval1Object(dbus.service.Object):
        """One object at ``/org/pantheon/HostApproval1`` exporting the
        four-method ``Reviewer``, two-method ``Consumer``, and
        one-method ``HumanIngress`` interfaces — exactly the ratified
        seven, no signals, no properties (ORG-052 §4/§5: evidence
        cannot leak through a broadcast channel)."""

        def __init__(
            self,
            connection: "dbus.bus.BusConnection",
            *,
            service: ApprovalService,
            approver_resolver: ApproverSubjectResolver,
            consumer_uid: int,
            ingress_uid: int,
        ) -> None:
            super().__init__(connection, wire.OBJECT_PATH)
            self._service = service
            self._approver_resolver = approver_resolver
            self._consumer_uid = int(consumer_uid)
            self._ingress_uid = int(ingress_uid)

        # ------------------------------------------------------------
        # transport-boundary identity (per interface, before any domain
        # call)
        # ------------------------------------------------------------

        def _get_unix_user(self, sender: str) -> int:
            return int(self.connection.get_unix_user(sender))

        def _subject_uid(self, sender: str | None) -> int:
            if not sender:
                raise _wire_error(PolicyDenied("subject_denied"))
            try:
                return self._get_unix_user(sender)
            except Exception as exc:
                # the bus could not attest the caller's credentials —
                # fail closed as an authorization refusal
                raise _wire_error(PolicyDenied("subject_denied")) from exc

        def _approver_subject(self, sender: str | None) -> str:
            uid = self._subject_uid(sender)
            try:
                subject = self._approver_resolver.subject_for_uid(uid)
            except Exception as exc:
                raise _wire_error(PolicyDenied("subject_denied")) from exc
            if subject is None:
                raise _wire_error(PolicyDenied("subject_denied"))
            return subject

        def _consumer_caller(self, sender: str | None) -> int:
            uid = self._subject_uid(sender)
            if uid != self._consumer_uid:
                raise _wire_error(PolicyDenied("subject_denied"))
            return uid

        def _ingress_caller(self, sender: str | None) -> int:
            uid = self._subject_uid(sender)
            if uid != self._ingress_uid:
                raise _wire_error(PolicyDenied("subject_denied"))
            return uid

        @contextmanager
        def _mapped_errors(self) -> Iterator[None]:
            try:
                yield
            except dbus.exceptions.DBusException:
                raise
            except Exception as exc:
                raise _wire_error(exc) from exc

        # ------------------------------------------------------------
        # org.pantheon.HostApproval1.Reviewer — ratified four
        # ------------------------------------------------------------

        @dbus.service.method(
            wire.REVIEWER_INTERFACE,
            in_signature="sss",
            out_signature="ssss",
            sender_keyword="sender",
        )
        def Review(
            self,
            plan_handle: str,
            expected_digest: str,
            request_plan_handle: str,
            sender: str | None = None,
        ) -> tuple[str, str, str, str]:
            """Slot 3 (the ADR's "recomputed digest") is
            ``record.plan_digest``: equal BY CONSTRUCTION to the honest
            recomputation on every return path — the domain raises if
            the broker-side recomputed summary digest diverges from the
            metadata digest or the caller's expectation, and the
            journal's replay key includes ``plan_digest``, so a
            replayed record can never carry a different digest
            (verified at source, T-2913 PO requirement 2).

            Wire ``""`` in ``request_plan_handle`` means a direct
            review (D-Bus has no null); ``handoff_anchor`` is not on
            the ratified wire and stays ``None`` here.
            """

            subject = self._approver_subject(sender)
            with self._mapped_errors():
                request = str(request_plan_handle)
                result = self._service.review(
                    subject,
                    str(plan_handle),
                    str(expected_digest),
                    request if request else None,
                )
                record = result.record
                return (
                    record.review_handle,
                    record.state,
                    record.plan_digest,
                    result.plan_summary,
                )

        @dbus.service.method(
            wire.REVIEWER_INTERFACE,
            in_signature="s",
            out_signature="ss",
            sender_keyword="sender",
        )
        def ReviewGet(
            self,
            review_handle: str,
            sender: str | None = None,
        ) -> tuple[str, str]:
            subject = self._approver_subject(sender)
            with self._mapped_errors():
                record = self._service.review_get(
                    subject,
                    str(review_handle),
                )
                return (record.state, wire.review_summary_json(record))

        @dbus.service.method(
            wire.REVIEWER_INTERFACE,
            in_signature="ssssss",
            out_signature="ss",
            sender_keyword="sender",
        )
        def Decide(
            self,
            review_handle: str,
            expected_digest: str,
            decision: str,
            reason_code: str,
            audit_anchor: str,
            idempotency_key: str,
            sender: str | None = None,
        ) -> tuple[str, str]:
            """Slot 2 is the opaque approval handle ONLY when granted;
            a denial returns the empty string (D-Bus has no null)."""

            subject = self._approver_subject(sender)
            with self._mapped_errors():
                result = self._service.decide(
                    subject,
                    str(review_handle),
                    str(expected_digest),
                    str(decision),
                    str(reason_code),
                    str(audit_anchor),
                    str(idempotency_key),
                )
                return (result.state, result.approval_handle or "")

        @dbus.service.method(
            wire.REVIEWER_INTERFACE,
            in_signature="sss",
            out_signature="ss",
            sender_keyword="sender",
        )
        def RequestHuman(
            self,
            review_handle: str,
            expected_digest: str,
            idempotency_key: str,
            sender: str | None = None,
        ) -> tuple[str, str]:
            # Exposed but refusing (broker Verify precedent): identity
            # first, so an unauthorized caller gets the authorization
            # refusal, never Unsupported.
            self._approver_subject(sender)
            raise _named_error(
                wire.UNSUPPORTED_ERROR,
                _UNSUPPORTED_HUMAN_REQUEST,
            )

        # ------------------------------------------------------------
        # org.pantheon.HostApproval1.Consumer — broker uid ONLY
        # ------------------------------------------------------------

        @dbus.service.method(
            wire.CONSUMER_INTERFACE,
            in_signature="ssss",
            out_signature="ss",
            sender_keyword="sender",
        )
        def ValidateAndBind(
            self,
            approval_handle: str,
            plan_handle: str,
            expected_digest: str,
            operation_handle: str,
            sender: str | None = None,
        ) -> tuple[str, str]:
            uid = self._consumer_caller(sender)
            with self._mapped_errors():
                outcome = self._service.validate_and_bind(
                    uid,
                    str(approval_handle),
                    str(plan_handle),
                    str(expected_digest),
                    str(operation_handle),
                )
                if outcome.state == "bound":
                    return wire.bind_success_slots(outcome)
                # value-to-error conversion at the transport tier ONLY
                # (OQ3(c) — see wire.bind_denial_reason's docstring)
                raise _named_error(
                    wire.BIND_DENIED_ERROR,
                    wire.bind_denial_reason(outcome),
                )

        @dbus.service.method(
            wire.CONSUMER_INTERFACE,
            in_signature="s",
            out_signature="ss",
            sender_keyword="sender",
        )
        def ApprovalStatus(
            self,
            approval_handle: str,
            sender: str | None = None,
        ) -> tuple[str, str]:
            uid = self._consumer_caller(sender)
            with self._mapped_errors():
                status = self._service.approval_status(
                    uid,
                    str(approval_handle),
                )
                return (status.state, wire.approval_status_json(status))

        # ------------------------------------------------------------
        # org.pantheon.HostApproval1.HumanIngress — ingress uid ONLY
        # ------------------------------------------------------------

        @dbus.service.method(
            wire.HUMAN_INGRESS_INTERFACE,
            in_signature="ssssss",
            out_signature="ss",
            sender_keyword="sender",
        )
        def HumanDecision(
            self,
            review_handle: str,
            expected_digest: str,
            decision: str,
            source_event_handle: str,
            callback_nonce: str,
            idempotency_key: str,
            sender: str | None = None,
        ) -> tuple[str, str]:
            # Exposed but refusing (T-2919): ingress identity first.
            self._ingress_caller(sender)
            raise _named_error(
                wire.UNSUPPORTED_ERROR,
                _UNSUPPORTED_HUMAN_DECISION,
            )
