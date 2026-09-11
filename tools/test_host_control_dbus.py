"""Tests for ORG-052 Unit 3a — the ``org.pantheon.HostBroker1`` D-Bus
service (T-2912).

Layout:

- Unguarded tests (error namespace totality, ApprovalSource provider,
  staged-artifact parses, structural reachability) run on any interpreter.
- dbus-guarded tests are decorated with ``skipUnless``; on the canonical
  host dbus-python IS importable under system python3, so a skip there
  means the guard is broken, not that the environment is missing.
- Private-bus integration tests spawn one dbus-daemon on a fixed socket
  path plus one service subprocess per TestCase class (never the real
  system bus, never ``dbus.SessionBus()`` — an in-process self-call
  deadlocks on the singleton).
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import tools.host_control.contracts as contracts
from tools.host_control import wire
from tools.host_control.approval_source import BrokerApprovalSource
from tools.host_control.contracts import (
    HostControlError,
    PolicyDenied,
    RegistryError,
    UnknownHandle,
    sha256_json,
)
from tools.host_control.planner import HostPlanService
from tools.test_host_control_dbus_driver import (
    MutableClock,
    build_domain,
    seed_other_subject,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOST_CONTROL = REPO_ROOT / "tools" / "host_control"

SUBJECT_UID = 1003
SEAT_B_UID = 60001


class ErrorNamespaceTests(unittest.TestCase):
    """The closed, versioned error namespace (locked input N5)."""

    def _hierarchy(self) -> set[type]:
        found: set[type] = {HostControlError}
        frontier = [HostControlError]
        while frontier:
            klass = frontier.pop()
            for child in klass.__subclasses__():
                if child not in found:
                    found.add(child)
                    frontier.append(child)
        return found

    def _construct_for_totality(self, klass: type) -> HostControlError:
        """Construct one instance of any HostControlError subclass.

        Most classes accept a bare message. A subclass may additionally
        require validated keyword-only arguments (e.g. HandoffMismatch's
        closed reason code); those are satisfied structurally — by
        probing whatever closed ``*_REASON_CODES`` frozenset the class's
        OWN defining module exposes — rather than special-casing the
        class by name, so a future required-kwarg subclass is covered
        automatically instead of needing a new branch here.
        """
        if klass is PolicyDenied:
            return klass("subject_denied")
        try:
            return klass("boom")
        except TypeError:
            pass
        signature = inspect.signature(klass.__init__)
        required = [
            name
            for name, param in signature.parameters.items()
            if param.kind is inspect.Parameter.KEYWORD_ONLY
            and param.default is inspect.Parameter.empty
        ]
        self.assertTrue(
            required,
            f"{klass.__name__} rejected a bare message for a reason "
            "other than a missing required keyword-only argument",
        )
        closed_sets = [
            value
            for name, value in vars(sys.modules[klass.__module__]).items()
            if name.endswith("REASON_CODES") and isinstance(value, frozenset)
        ]
        for candidate_set in closed_sets:
            for candidate in sorted(candidate_set):
                try:
                    return klass("boom", **{n: candidate for n in required})
                except HostControlError:
                    continue
        self.fail(
            f"{klass.__name__} requires keyword-only {required} but no "
            f"closed reason-code set in {klass.__module__} satisfies it"
        )

    def test_each_contract_class_maps_to_its_own_exact_name(self) -> None:
        # Mutation probe iii (post-green) proved closed-set MEMBERSHIP
        # alone is vacuous for subclasses: dropping a subclass mapping
        # silently degrades it to Failed via the ancestor fallback. So
        # the ten contract classes are pinned to their EXACT names.
        expected = {
            contracts.PolicyDenied: wire.POLICY_DENIED_ERROR,
            contracts.IdempotencyConflict: wire.IDEMPOTENCY_CONFLICT_ERROR,
            contracts.UnknownHandle: wire.UNKNOWN_HANDLE_ERROR,
            contracts.SubjectMismatch: wire.SUBJECT_MISMATCH_ERROR,
            contracts.InvalidTransition: wire.INVALID_TRANSITION_ERROR,
            contracts.ApprovalUnavailable: wire.APPROVAL_UNAVAILABLE_ERROR,
            contracts.RecoveryRefused: wire.RECOVERY_REFUSED_ERROR,
            RegistryError: wire.REGISTRY_DENIED_ERROR,
            contracts.MigrationRequired: wire.MIGRATION_REQUIRED_ERROR,
            HostControlError: wire.FAILED_ERROR,
        }
        for klass, name in expected.items():
            if klass is PolicyDenied:
                instance = klass("subject_denied")
            else:
                instance = klass("boom")
            self.assertEqual(wire.error_name_for(instance), name, klass)
        # and each name is distinct: no two contract classes share one
        self.assertEqual(len(set(expected.values())), len(expected))

    def test_error_mapping_is_total_over_the_live_exception_hierarchy(
        self,
    ) -> None:
        hierarchy = self._hierarchy()
        # The mapping is structural: every class in the LIVE hierarchy —
        # including any subclass added after this unit — must resolve to
        # exactly one closed name via nearest-ancestor resolution.
        self.assertGreaterEqual(len(hierarchy), 10)
        for klass in hierarchy:
            instance = self._construct_for_totality(klass)
            name = wire.error_name_for(instance)
            self.assertIn(
                name,
                wire.CLOSED_ERROR_NAMES,
                f"{klass.__name__} resolved outside the closed set",
            )
            # deterministic: resolving twice yields the same name
            self.assertEqual(name, wire.error_name_for(instance))
        # a NEW subclass minted at runtime inherits its ancestor's name
        fresh = type("FreshDenial", (PolicyDenied,), {})
        self.assertEqual(
            wire.error_name_for(fresh("subject_denied")),
            wire.error_name_for(PolicyDenied("subject_denied")),
        )

    def test_unmapped_exceptions_become_bounded_internal_error(self) -> None:
        secret = "/etc/shadow token=hunter2 " + "x" * 4096
        for exc in (
            TypeError(secret),
            ValueError(secret),
            RuntimeError(secret),
            Exception(secret),
        ):
            self.assertEqual(
                wire.error_name_for(exc),
                wire.INTERNAL_ERROR,
            )
            body = wire.error_body_for(exc)
            self.assertEqual(body, "internal error")
            self.assertNotIn("hunter2", body)
            self.assertNotIn("/etc/shadow", body)

    def test_registry_faults_carry_a_fixed_non_disclosing_body(self) -> None:
        # RegistryError bodies can name trusted-file mode/path facts —
        # server configuration a caller must not learn (PO-required
        # change, T-2912 plan approval).
        exc = RegistryError("manifest path is outside trusted roots /root/x")
        self.assertEqual(wire.error_name_for(exc), wire.REGISTRY_DENIED_ERROR)
        body = wire.error_body_for(exc)
        self.assertEqual(body, "registry refused the request")
        self.assertNotIn("/root", body)

    def test_policy_denied_body_is_the_reason_code_only(self) -> None:
        exc = PolicyDenied("target_denied")
        self.assertEqual(wire.error_name_for(exc), wire.POLICY_DENIED_ERROR)
        self.assertEqual(wire.error_body_for(exc), "target_denied")

    def test_domain_error_bodies_are_bounded(self) -> None:
        exc = HostControlError("y" * 4096)
        self.assertEqual(wire.error_name_for(exc), wire.FAILED_ERROR)
        self.assertLessEqual(len(wire.error_body_for(exc)), 200)

    def test_closed_error_namespace_is_versioned_and_exact(self) -> None:
        expected = frozenset(
            f"org.pantheon.HostBroker1.Error.{suffix}"
            for suffix in (
                "PolicyDenied",
                "IdempotencyConflict",
                "UnknownHandle",
                "SubjectMismatch",
                "InvalidTransition",
                "ApprovalUnavailable",
                "RecoveryRefused",
                "RegistryDenied",
                "MigrationRequired",
                "Failed",
                "Unsupported",
                "Internal",
            )
        )
        self.assertEqual(wire.CLOSED_ERROR_NAMES, expected)
        for name in wire.CLOSED_ERROR_NAMES:
            self.assertTrue(
                name.startswith("org.pantheon.HostBroker1.Error."),
                name,
            )

    def test_wire_constants_match_the_ratified_names(self) -> None:
        self.assertEqual(wire.BUS_NAME, "org.pantheon.HostBroker1")
        self.assertEqual(wire.OBJECT_PATH, "/org/pantheon/HostBroker1")
        self.assertEqual(
            wire.MANAGER_INTERFACE,
            "org.pantheon.HostBroker1.Manager",
        )
        self.assertEqual(
            wire.APPROVAL_SOURCE_INTERFACE,
            "org.pantheon.HostBroker1.ApprovalSource",
        )


class FamilyFlatteningTests(unittest.TestCase):
    """Comma-join is unambiguous ONLY because families are
    ``_IDENTIFIER``-constrained (no commas); this pins the pair so a
    future charset relaxation fails loudly (plan composition entry)."""

    def test_family_flattening_round_trips(self) -> None:
        families = ("persona.install", "persona.verify")
        flattened = wire.flatten_families(families)
        self.assertEqual(flattened, "persona.install,persona.verify")
        self.assertEqual(tuple(flattened.split(",")), families)

    def test_family_flattening_refuses_comma_bearing_or_empty_names(
        self,
    ) -> None:
        for bad in (("a,b",), ("",), ("ok", "with,comma")):
            with self.assertRaises(HostControlError):
                wire.flatten_families(bad)
        with self.assertRaises(HostControlError):
            wire.flatten_families(())


class CapabilitiesJsonTests(unittest.TestCase):
    def _result(self) -> contracts.CapabilitiesResult:
        return contracts.CapabilitiesResult(
            contract_version="org.pantheon.HostBroker1/v1",
            broker_revision="unit-test",
            policy_revision="policy-r1",
            health="ready",
            host_operation_families=("persona.install", "persona.verify"),
            grants=(
                contracts.CapabilityGrant(
                    action="plan",
                    target_seat_id="recovery",
                    operation_families=("persona.install",),
                    approval_class="human",
                ),
            ),
        )

    def test_capabilities_json_is_canonical_schema_versioned_and_bounded(
        self,
    ) -> None:
        import json

        encoded = wire.capabilities_json(self._result())
        raw = json.loads(encoded)
        self.assertEqual(encoded, contracts.canonical_json(raw))
        self.assertEqual(raw["schema"], "pantheon-host-capabilities/v1")
        self.assertEqual(raw["health"], "ready")
        self.assertEqual(raw["policy_revision"], "policy-r1")
        self.assertEqual(
            raw["grants"],
            [
                {
                    "action": "plan",
                    "target_seat_id": "recovery",
                    "operation_families": ["persona.install"],
                    "approval_class": "human",
                }
            ],
        )
        self.assertLessEqual(len(encoded.encode("utf-8")), 65536)


class ProviderCase(unittest.TestCase):
    """Shared domain fixture for the ApprovalSource provider tests."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REPO_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.clock = MutableClock()
        self.fixture = build_domain(
            Path(self.temp.name) / "domain",
            subject_uid=SUBJECT_UID,
            clock=self.clock,
        )
        self.addCleanup(self.fixture.close)
        self.plan_result = self.fixture.plan_service.plan(
            SUBJECT_UID,
            "recovery",
            "manifest-r1",
            "provider-plan-1",
        )
        self.provider = BrokerApprovalSource(
            journal=self.fixture.journal,
            clock=self.clock,
        )


class ApprovalSourceProviderTests(ProviderCase):
    def test_provider_returns_exact_metadata_for_live_plan(self) -> None:
        subject = self.fixture.registry.subject_for_uid(SUBJECT_UID)
        stored = self.fixture.journal.get_plan(
            subject=subject,
            plan_handle=self.plan_result.plan_handle,
        )
        metadata = self.provider.plan_metadata_wire(
            self.plan_result.plan_handle
        )
        self.assertEqual(
            metadata,
            (
                "alpha",  # owner: the plan owner's seat_id
                "alpha",  # performer: a broker plan's performer IS its owner
                "recovery",
                "persona.install",
                stored.digest,
                stored.effect_digest,
                "manifest-r1",
                "policy-r1",
            ),
        )
        self.assertEqual(len(metadata), 8)
        for item in metadata:
            self.assertIsInstance(item, str)
            self.assertTrue(item)

    def test_provider_treats_expired_plan_as_unknown(self) -> None:
        # C1 disposition: the 8-field wire has no staleness slot, so an
        # error is the only fail-closed channel, and the error must be
        # INDISTINGUISHABLE from never-existed.
        self.clock.advance(timedelta(hours=2))
        with self.assertRaises(UnknownHandle) as expired:
            self.provider.plan_metadata_wire(self.plan_result.plan_handle)
        with self.assertRaises(UnknownHandle) as unknown:
            self.provider.plan_metadata_wire("f" * 64)
        self.assertEqual(str(expired.exception), str(unknown.exception))
        with self.assertRaises(UnknownHandle):
            self.provider.plan_summary(
                self.plan_result.plan_handle,
                self.plan_result.plan_digest,
            )

    def test_provider_rejects_unknown_and_malformed_handles(self) -> None:
        with self.assertRaises(UnknownHandle):
            self.provider.plan_metadata_wire("e" * 64)
        for malformed in ("", "short", "Z" * 64, "x" * 65):
            with self.assertRaises(UnknownHandle):
                self.provider.plan_metadata_wire(malformed)

    def test_provider_summary_recomputes_digest_and_matches_planner(
        self,
    ) -> None:
        digest, summary = self.provider.plan_summary(
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
        )
        subject = self.fixture.registry.subject_for_uid(SUBJECT_UID)
        stored = self.fixture.journal.get_plan(
            subject=subject,
            plan_handle=self.plan_result.plan_handle,
        )
        # honest recomputation, not an echo of the stored column
        self.assertEqual(digest, sha256_json(stored.digest_payload()))
        self.assertEqual(digest, self.plan_result.plan_digest)
        # single canonical redaction surface (OQ2 disposition): the
        # ApprovalSource summary bytes ARE the planner summary bytes
        self.assertEqual(summary, HostPlanService.plan_summary(stored))
        self.assertEqual(summary, self.plan_result.summary_json)

    def test_provider_summary_refuses_digest_mismatch(self) -> None:
        with self.assertRaises(HostControlError) as caught:
            self.provider.plan_summary(
                self.plan_result.plan_handle,
                "a" * 64,
            )
        self.assertNotIsInstance(caught.exception, UnknownHandle)

    def test_provider_discloses_no_credentials_paths_or_roster(self) -> None:
        # Gate 12 broker half: closed key-set enumeration, not a
        # blocklist sample.
        metadata = self.provider.plan_metadata_wire(
            self.plan_result.plan_handle
        )
        joined = "|".join(metadata)
        for forbidden in ("uid", "/home/", "/projection", "account"):
            self.assertNotIn(forbidden, joined)
        _, summary = self.provider.plan_summary(
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
        )
        raw = json.loads(summary)
        self.assertEqual(
            set(raw),
            {
                "schema",
                "plan_handle",
                "plan_digest",
                "effect_digest",
                "target_seat_id",
                "manifest_revision",
                "manifest_sha256",
                "framework_revision",
                "framework_sha256",
                "policy_revision",
                "approval_class",
                "operation_families",
                "operations",
                "expires_at",
            },
        )
        for operation in raw["operations"]:
            self.assertEqual(
                set(operation),
                {"operation_id", "kind", "layer", "privilege"},
            )
        for forbidden in ("uid", "install_root", "account", "executor"):
            self.assertNotIn(forbidden, summary)


class UnboundPlanReadTests(ProviderCase):
    def test_journal_loads_plan_unbound_for_approval_source(self) -> None:
        plan = self.fixture.journal.load_plan_unbound(
            self.plan_result.plan_handle
        )
        self.assertEqual(plan.plan_handle, self.plan_result.plan_handle)
        self.assertEqual(plan.digest, self.plan_result.plan_digest)
        self.assertTrue(plan.validate_digest())
        with self.assertRaises(UnknownHandle):
            self.fixture.journal.load_plan_unbound("d" * 64)

    def test_provider_reads_other_subjects_plans(self) -> None:
        # The whole point of the unbound read: the approval service is
        # never the plan owner.
        seeded = seed_other_subject(self.fixture, seat_b_uid=SEAT_B_UID)
        metadata = self.provider.plan_metadata_wire(
            seeded["seat_b_plan_handle"]
        )
        self.assertEqual(metadata[0], "bravo")
        self.assertEqual(metadata[1], "bravo")


def _is_approval_package(module_name: str) -> bool:
    """True only for the approval PACKAGE and its submodules — NOT for
    the broker's ``approval_source`` module, whose name shares the
    prefix (the substring check was a false positive caught at RED)."""

    return module_name == "tools.host_control.approval" or (
        module_name.startswith("tools.host_control.approval.")
    )


def _assert_no_approval_package_import(
    case: unittest.TestCase,
    path: Path,
) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                case.assertFalse(
                    _is_approval_package(alias.name),
                    f"{path.name} imports {alias.name}",
                )
        elif isinstance(node, ast.ImportFrom):
            case.assertFalse(
                _is_approval_package(node.module or ""),
                f"{path.name} imports from {node.module}",
            )


class StructuralReachabilityTests(unittest.TestCase):
    """Structural guards over the production surface (PO conditions on
    the authorized journal edit; gate 18 broker half)."""

    def _production_modules(self) -> dict[str, ast.Module]:
        modules = {}
        for path in sorted(HOST_CONTROL.glob("*.py")):
            modules[path.name] = ast.parse(
                path.read_text(encoding="utf-8")
            )
        return modules

    def test_unbound_plan_read_is_reachable_only_from_approval_source(
        self,
    ) -> None:
        referencing: set[str] = set()
        for name, tree in self._production_modules().items():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "load_plan_unbound"
                ):
                    referencing.add(name)
                if (
                    isinstance(node, ast.FunctionDef)
                    and node.name == "load_plan_unbound"
                ):
                    referencing.add(f"def:{name}")
        self.assertEqual(
            referencing,
            {"def:journal.py", "approval_source.py"},
            "the unbound plan read must be defined in journal.py and "
            "called only from approval_source.py — no Manager path",
        )

    def test_broker_dbus_modules_import_no_approval_package(self) -> None:
        # Gate 18 broker half, structural: the broker service cannot
        # write approval state because it never even imports the
        # approval package.
        for module_name in ("wire.py", "approval_source.py"):
            _assert_no_approval_package_import(
                self,
                HOST_CONTROL / module_name,
            )


from tools.host_control import dbus_service  # noqa: E402  (import-safe: dbus guarded inside)

requires_dbus = unittest.skipUnless(
    dbus_service.DBUS_AVAILABLE,
    "dbus-python not importable — on the canonical host this skip means "
    "the guard is broken, not that the environment is missing (N9)",
)

# The ratified inventory, transcribed INDEPENDENTLY from ORG-052 §4/§5
# (deliberately not imported from wire.py, so a drift in the production
# constant cannot self-certify): (interface, method) -> (arg names, in, out).
RATIFIED_INVENTORY = {
    ("org.pantheon.HostBroker1.Manager", "Capabilities"): ((), "", "ss"),
    ("org.pantheon.HostBroker1.Manager", "Plan"): (
        ("seat_id", "manifest_revision", "idempotency_key"),
        "sss",
        "sss",
    ),
    ("org.pantheon.HostBroker1.Manager", "Apply"): (
        ("plan_handle", "plan_digest", "idempotency_key", "approval_handle"),
        "ssss",
        "s",
    ),
    ("org.pantheon.HostBroker1.Manager", "Verify"): (
        ("seat_id", "plan_handle", "idempotency_key"),
        "sss",
        "s",
    ),
    ("org.pantheon.HostBroker1.Manager", "Rollback"): (
        ("receipt_handle", "idempotency_key", "approval_handle"),
        "sss",
        "s",
    ),
    ("org.pantheon.HostBroker1.Manager", "OperationGet"): (
        ("operation_handle",),
        "s",
        "ss",
    ),
    ("org.pantheon.HostBroker1.Manager", "ApprovalGet"): (
        ("approval_handle",),
        "s",
        "ss",
    ),
    ("org.pantheon.HostBroker1.ApprovalSource", "PlanMetadata"): (
        ("plan_handle",),
        "s",
        "ssssssss",
    ),
    ("org.pantheon.HostBroker1.ApprovalSource", "PlanSummary"): (
        ("plan_handle", "expected_digest"),
        "ss",
        "ss",
    ),
}

# Identity may NEVER be a caller argument (locked N6 / ORG-052 §2).
IDENTITY_ARG_NAMES = frozenset(
    (
        "uid",
        "subject",
        "subject_uid",
        "caller",
        "caller_uid",
        "persona",
        "role",
        "instance",
        "account",
        "user",
        "approver",
        "performer",
        "owner",
        "seat_uid",
    )
)


def _exported_methods() -> tuple[dict, dict]:
    """Partition exported members into (pantheon, inherited-standard).

    The library base class ships its own decorated members (e.g.
    ``Introspect`` on ``org.freedesktop.DBus.Introspectable``) — the
    class-tier shape of substrate finding 5. The exactness assertion is
    therefore scoped: OUR class defines exactly the ratified nine, and
    every foreign member is a standard org.freedesktop.DBus.* one
    inherited from the base, never defined or overridden by us.
    """

    pantheon: dict = {}
    foreign: dict = {}
    own_names = set(vars(dbus_service.HostBroker1Object))
    for name in dir(dbus_service.HostBroker1Object):
        member = getattr(dbus_service.HostBroker1Object, name)
        if getattr(member, "_dbus_is_signal", False):
            raise AssertionError(f"signal exported: {name}")
        if not getattr(member, "_dbus_is_method", False):
            continue
        interface = member._dbus_interface
        if interface.startswith("org.pantheon."):
            pantheon[(interface, name)] = member
            continue
        if not interface.startswith("org.freedesktop.DBus."):
            raise AssertionError(
                f"unexpected non-standard interface: {interface}.{name}"
            )
        if name in own_names:
            raise AssertionError(
                f"standard member defined/overridden locally: {name}"
            )
        foreign[(interface, name)] = member
    return pantheon, foreign


class FakeConnectionRefuses:
    def get_unix_user(self, sender: str) -> int:
        raise RuntimeError("bus cannot resolve sender credentials")


class FakeRegistryUnregistered:
    def subject_for_uid(self, uid: int):
        raise RegistryError("authenticated uid is not registered")


class FakeRegistryResolves:
    def __init__(self, subject: object = "subject") -> None:
        self.subject = subject

    def subject_for_uid(self, uid: int):
        return self.subject


class RefusingOperations:
    def prepare_verify(self, *args: object, **kwargs: object):
        raise HostControlError(
            "verify operations are not implemented in this unit"
        )


class ExplodingPlanService:
    def capabilities(self, uid: int):
        raise TypeError("secret /etc/path leak candidate")


def _bare_service(**attrs: object) -> object:
    """Instance without dbus export machinery, for negative paths a live
    bus cannot produce (e.g. credential-resolution failure)."""

    instance = object.__new__(dbus_service.HostBroker1Object)
    for key, value in attrs.items():
        setattr(instance, key, value)
    return instance


@requires_dbus
class HandlerSurfaceTests(unittest.TestCase):
    """In-process assertions over the exported surface (gates 2/9/17
    halves that need no live bus)."""

    def test_declared_signatures_match_the_ratified_tables_exactly(
        self,
    ) -> None:
        pantheon, foreign = _exported_methods()
        observed = {
            key: (
                tuple(member._dbus_args),
                member._dbus_in_signature,
                member._dbus_out_signature,
            )
            for key, member in pantheon.items()
        }
        expected = dict(RATIFIED_INVENTORY)
        self.assertEqual(observed, expected)
        # scalar-only inputs: no dict/variant/array/passthrough slot
        for _, in_signature, _out in observed.values():
            self.assertTrue(set(in_signature) <= {"s"}, in_signature)
        # the inherited standard surface is exactly the library base's
        for interface, name in foreign:
            self.assertTrue(
                interface.startswith("org.freedesktop.DBus."),
                f"{interface}.{name}",
            )

    def test_no_method_accepts_caller_identity_arguments(self) -> None:
        pantheon, _foreign = _exported_methods()
        for (interface, name), member in pantheon.items():
            self.assertEqual(
                member._dbus_sender_keyword,
                "sender",
                f"{interface}.{name} must resolve the live bus sender",
            )
            declared = set(member._dbus_args)
            self.assertFalse(
                declared & IDENTITY_ARG_NAMES,
                f"{interface}.{name} exposes an identity argument",
            )

    def test_unresolvable_sender_fails_closed(self) -> None:
        import dbus as dbus_module

        instance = _bare_service(_registry=FakeRegistryResolves())
        instance._get_unix_user = FakeConnectionRefuses().get_unix_user
        for sender in (":1.42", None, ""):
            with self.assertRaises(dbus_module.DBusException) as caught:
                dbus_service.HostBroker1Object.Capabilities(
                    instance,
                    sender=sender,
                )
            self.assertEqual(
                caught.exception.get_dbus_name(),
                wire.POLICY_DENIED_ERROR,
            )
            self.assertEqual(
                caught.exception.get_dbus_message(),
                "subject_denied",
            )

    def test_unregistered_uid_is_policy_denied_not_registry_fault(
        self,
    ) -> None:
        # PO-required change: RegistryError from subject_for_uid at the
        # transport gate is an AUTHORIZATION refusal (PolicyDenied),
        # never RegistryDenied — that name is reserved for genuine
        # root-owned-config faults.
        import dbus as dbus_module

        instance = _bare_service(_registry=FakeRegistryUnregistered())
        instance._get_unix_user = lambda sender: 4242
        with self.assertRaises(dbus_module.DBusException) as caught:
            dbus_service.HostBroker1Object.Plan(
                instance,
                "recovery",
                "manifest-r1",
                "key-1",
                sender=":1.9",
            )
        self.assertEqual(
            caught.exception.get_dbus_name(),
            wire.POLICY_DENIED_ERROR,
        )
        self.assertEqual(
            caught.exception.get_dbus_message(),
            "subject_denied",
        )

    def test_verify_maps_domain_refusal_to_unsupported(self) -> None:
        import dbus as dbus_module

        instance = _bare_service(
            _registry=FakeRegistryResolves(),
            _operation_service=RefusingOperations(),
        )
        instance._get_unix_user = lambda sender: 1003
        with self.assertRaises(dbus_module.DBusException) as caught:
            dbus_service.HostBroker1Object.Verify(
                instance,
                "recovery",
                "a" * 64,
                "key-1",
                sender=":1.9",
            )
        self.assertEqual(
            caught.exception.get_dbus_name(),
            wire.UNSUPPORTED_ERROR,
        )

    def test_internal_errors_never_carry_tracebacks_or_messages(
        self,
    ) -> None:
        import dbus as dbus_module

        instance = _bare_service(
            _registry=FakeRegistryResolves(),
            _plan_service=ExplodingPlanService(),
        )
        instance._get_unix_user = lambda sender: 1003
        with self.assertRaises(dbus_module.DBusException) as caught:
            dbus_service.HostBroker1Object.Capabilities(
                instance,
                sender=":1.9",
            )
        self.assertEqual(
            caught.exception.get_dbus_name(),
            wire.INTERNAL_ERROR,
        )
        self.assertEqual(
            caught.exception.get_dbus_message(),
            "internal error",
        )

    def test_approval_source_gate_refuses_non_approval_uid(self) -> None:
        import dbus as dbus_module

        instance = _bare_service(_approval_uid=7001)
        instance._get_unix_user = lambda sender: 7002
        with self.assertRaises(dbus_module.DBusException) as caught:
            dbus_service.HostBroker1Object.PlanMetadata(
                instance,
                "a" * 64,
                sender=":1.9",
            )
        self.assertEqual(
            caught.exception.get_dbus_name(),
            wire.POLICY_DENIED_ERROR,
        )


class DbusModuleStructureTests(unittest.TestCase):
    """Unguarded structure checks over dbus_service.py source."""

    def test_dbus_service_imports_no_approval_package(self) -> None:
        _assert_no_approval_package_import(
            self,
            HOST_CONTROL / "dbus_service.py",
        )

    def test_dbus_service_never_touches_the_unbound_plan_read(self) -> None:
        source = (HOST_CONTROL / "dbus_service.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("load_plan_unbound", source)

    def test_approval_status_seam_is_value_returning(self) -> None:
        # Gate 18 broker half: the seam takes no journal, connection, or
        # callback — the type surface, not a convention.
        import inspect

        signature = inspect.signature(
            dbus_service.ApprovalStatusSource.approval_status
        )
        self.assertEqual(
            list(signature.parameters),
            ["self", "subject", "approval_handle"],
        )


import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import xml.etree.ElementTree as ElementTree  # noqa: E402

import tools._host_control_testbus as testbus  # noqa: E402

DBUS_DAEMON = shutil.which("dbus-daemon")

requires_private_bus = unittest.skipUnless(
    dbus_service.DBUS_AVAILABLE and DBUS_DAEMON,
    "private-bus integration needs dbus-python and dbus-daemon",
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


class PrivateBusCase(unittest.TestCase):
    """One private dbus-daemon + one service subprocess per class.

    The permissive bus config is DELIBERATE (gate 10): every refusal
    these tests observe is the service's own server-side authorization,
    never bus policy — bus policy is defence in depth, not the
    perimeter.
    """

    REGISTER_SUBJECT = True
    APPROVAL_UID_IS_CALLER = False

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._bus_handle = testbus.spawn_daemon("t2912-", _PERMISSIVE_BUS_CONFIG)
        cls.bus_dir = cls._bus_handle.bus_dir
        cls.daemon_log = cls._bus_handle.log
        cls.daemon = cls._bus_handle.process
        cls.bus_address = cls._bus_handle.bus_address
        cls.work = tempfile.TemporaryDirectory(dir=REPO_ROOT)

        approval_uid = (
            os.getuid()
            if cls.APPROVAL_UID_IS_CALLER
            else os.getuid() + 54321
        )
        root = Path(cls.work.name) / "domain"
        config = {
            "bus_address": cls.bus_address,
            "root": str(root),
            "subject_uid": os.getuid(),
            "register_subject": cls.REGISTER_SUBJECT,
            "approval_uid": approval_uid,
        }
        config_path = os.path.join(cls.bus_dir, "driver.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        cls.driver_log = open(  # noqa: SIM115 - closed in tearDownClass
            os.path.join(cls.bus_dir, "driver.log"), "w", encoding="utf-8"
        )
        cls.driver = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tools.test_host_control_dbus_driver",
                config_path,
            ],
            cwd=REPO_ROOT,
            stdout=cls.driver_log,
            stderr=cls.driver_log,
            preexec_fn=testbus.pdeathsig_preexec_fn(),
        )
        cls._bus_handle.register_driver(cls.driver.pid)

        import dbus
        import dbus.bus

        cls.connection = dbus.bus.BusConnection(cls.bus_address)
        deadline = time.monotonic() + 30
        while not cls.connection.name_has_owner(wire.BUS_NAME):
            if time.monotonic() > deadline:
                raise AssertionError(
                    "service never owned the broker name; driver log: "
                    + Path(cls.driver_log.name).read_text(encoding="utf-8")[
                        -2000:
                    ]
                )
            if cls.driver.poll() is not None:
                raise AssertionError(
                    "driver exited early; log: "
                    + Path(cls.driver_log.name).read_text(encoding="utf-8")[
                        -2000:
                    ]
                )
            time.sleep(0.05)
        state_path = root / "state.json"
        deadline = time.monotonic() + 20
        while not state_path.exists():
            if time.monotonic() > deadline:
                raise AssertionError("driver never wrote its state file")
            time.sleep(0.05)
        cls.state = json.loads(state_path.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        driver = getattr(cls, "driver", None)
        if driver is not None and driver.poll() is None:
            driver.terminate()
            try:
                driver.wait(timeout=10)
            except subprocess.TimeoutExpired:
                driver.kill()
                driver.wait(timeout=10)
        if getattr(cls, "driver_log", None) is not None:
            cls.driver_log.close()
        if getattr(cls, "work", None) is not None:
            cls.work.cleanup()
        if getattr(cls, "_bus_handle", None) is not None:
            testbus.stop_daemon(cls._bus_handle)
        super().tearDownClass()

    @classmethod
    def _call(
        cls,
        method: str,
        signature: str,
        args: tuple,
        interface: str = wire.MANAGER_INTERFACE,
    ):
        return cls.connection.call_blocking(
            wire.BUS_NAME,
            wire.OBJECT_PATH,
            interface,
            method,
            signature,
            args,
        )

    def assert_wire_error(
        self,
        expected_name: str,
        method: str,
        signature: str,
        args: tuple,
        interface: str = wire.MANAGER_INTERFACE,
    ) -> "Exception":
        import dbus

        with self.assertRaises(dbus.exceptions.DBusException) as caught:
            self._call(method, signature, args, interface)
        self.assertEqual(
            caught.exception.get_dbus_name(),
            expected_name,
            f"{interface}.{method}",
        )
        return caught.exception


@requires_private_bus
class ManagerBusTests(PrivateBusCase):
    """Registered caller on a permissive private bus (gates 2/9/15/17,
    N2/N3/N4, wire-tier gate 24)."""

    def test_introspection_exposes_exactly_the_locked_inventory(
        self,
    ) -> None:
        xml = self._call(
            "Introspect",
            "",
            (),
            "org.freedesktop.DBus.Introspectable",
        )
        root = ElementTree.fromstring(str(xml))
        interfaces = {
            node.get("name"): node for node in root.findall("interface")
        }
        pantheon = {
            name for name in interfaces if name.startswith("org.pantheon.")
        }
        self.assertEqual(
            pantheon,
            {wire.MANAGER_INTERFACE, wire.APPROVAL_SOURCE_INTERFACE},
        )
        # every other interface is a standard bus one (substrate
        # finding 5: Introspectable and friends appear alongside ours)
        for name in set(interfaces) - pantheon:
            self.assertTrue(name.startswith("org.freedesktop.DBus."), name)

        def observed(interface_name: str) -> dict:
            table = {}
            for method in interfaces[interface_name].findall("method"):
                in_signature = ""
                out_signature = ""
                for arg in method.findall("arg"):
                    direction = arg.get("direction", "in")
                    if direction == "in":
                        in_signature += arg.get("type")
                    else:
                        out_signature += arg.get("type")
                table[method.get("name")] = (in_signature, out_signature)
            return table

        self.assertEqual(
            observed(wire.MANAGER_INTERFACE),
            {
                "Capabilities": ("", "ss"),
                "Plan": ("sss", "sss"),
                "Apply": ("ssss", "s"),
                "Verify": ("sss", "s"),
                "Rollback": ("sss", "s"),
                "OperationGet": ("s", "ss"),
                "ApprovalGet": ("s", "ss"),
            },
        )
        self.assertEqual(
            observed(wire.APPROVAL_SOURCE_INTERFACE),
            {
                "PlanMetadata": ("s", "ssssssss"),
                "PlanSummary": ("ss", "ss"),
            },
        )
        # no broadcast signals and no properties on either interface
        # (ORG-052 §4; dbus-python cannot mint a property without
        # explicit machinery neither provider wires, but that was a
        # library-implied claim — pin it explicitly, T-2937)
        for name in pantheon:
            self.assertEqual(interfaces[name].findall("signal"), [])
            self.assertEqual(interfaces[name].findall("property"), [])

    def test_direct_plan_then_apply_stops_at_prepared(self) -> None:
        # direct Plan without a Capabilities preflight (gate 9 clause)
        handle, digest, summary = self._call(
            "Plan",
            "sss",
            ("recovery", "manifest-r1", "bus-plan-1"),
        )
        self.assertRegex(str(handle), r"^[0-9a-f]{64}$")
        self.assertRegex(str(digest), r"^[0-9a-f]{64}$")
        raw = json.loads(str(summary))
        self.assertEqual(raw["schema"], "pantheon-host-plan-summary/v1")
        self.assertEqual(raw["target_seat_id"], "recovery")

        operation_handle = self._call(
            "Apply",
            "ssss",
            (str(handle), str(digest), "bus-apply-1", "approval-x"),
        )
        state, record_json = self._call(
            "OperationGet",
            "s",
            (str(operation_handle),),
        )
        # PREPARE-only (N4): if the transport had chained into
        # authorize, the driver's tripwire binder would have failed the
        # Apply call itself, and the durable state would not be
        # "prepared".
        self.assertEqual(str(state), "prepared")
        record = json.loads(str(record_json))
        self.assertEqual(record["kind"], "apply")
        self.assertEqual(record["plan_handle"], str(handle))
        self.assertEqual(record["owner"]["seat_id"], "alpha")

    def test_rollback_prepares_against_own_complete_receipt(self) -> None:
        operation_handle = self._call(
            "Rollback",
            "sss",
            (
                self.state["subject_complete_receipt_handle"],
                "bus-rollback-1",
                "approval-y",
            ),
        )
        state, record_json = self._call(
            "OperationGet",
            "s",
            (str(operation_handle),),
        )
        self.assertEqual(str(state), "prepared")
        self.assertEqual(json.loads(str(record_json))["kind"], "rollback")

    def test_capabilities_scoped_despite_adversarial_environment(
        self,
    ) -> None:
        # Gate 9: identity comes from the bus uid; environment and
        # claimed persona cannot move it. (The supplementary-groups arm
        # needs root and lands with T-2920's disposable-host leg.)
        adversarial = {
            "PANTHEON_PERSONA": "bravo",
            "PANTHEON_SEAT": "bravo",
            "USER": "bravo",
            "SUDO_USER": "bravo",
        }
        saved = {key: os.environ.get(key) for key in adversarial}
        try:
            os.environ.update(adversarial)
            version, payload = self._call("Capabilities", "", ())
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(str(version), "org.pantheon.HostBroker1/v1")
        raw = json.loads(str(payload))
        self.assertEqual(raw["schema"], "pantheon-host-capabilities/v1")
        # only this subject's intersection: bravo has grants in the
        # policy file, and none of them may appear here
        self.assertEqual(
            raw["grants"],
            [
                {
                    "action": "plan",
                    "approval_class": "human",
                    "operation_families": ["persona.install"],
                    "target_seat_id": "recovery",
                }
            ],
        )
        self.assertNotIn("bravo", str(payload))
        self.assertNotIn("alpha", str(payload))  # no self-roster either

    def test_verify_is_exposed_but_unsupported(self) -> None:
        error = self.assert_wire_error(
            wire.UNSUPPORTED_ERROR,
            "Verify",
            "sss",
            ("recovery", "a" * 64, "bus-verify-1"),
        )
        self.assertIn("T-2917", error.get_dbus_message())

    def test_stolen_handles_are_denied_across_subjects(self) -> None:
        # Gate 15 broker half: seat-bravo artifacts seeded server-side;
        # the caller holds valid handles AND the digest and is still
        # refused — a handle or digest alone is not authority.
        self.assert_wire_error(
            wire.SUBJECT_MISMATCH_ERROR,
            "Apply",
            "ssss",
            (
                self.state["seat_b_plan_handle"],
                self.state["seat_b_plan_digest"],
                "bus-steal-1",
                "approval-z",
            ),
        )
        self.assert_wire_error(
            wire.SUBJECT_MISMATCH_ERROR,
            "OperationGet",
            "s",
            (self.state["seat_b_operation_handle"],),
        )
        self.assert_wire_error(
            wire.SUBJECT_MISMATCH_ERROR,
            "Rollback",
            "sss",
            (
                self.state["seat_b_receipt_handle"],
                "bus-steal-2",
                "approval-z",
            ),
        )

    def test_error_names_round_trip_verbatim(self) -> None:
        error = self.assert_wire_error(
            wire.UNKNOWN_HANDLE_ERROR,
            "OperationGet",
            "s",
            ("f" * 64,),
        )
        self.assertEqual(error.get_dbus_message(), "unknown operation handle")

    def test_plan_idempotency_replay_and_conflict(self) -> None:
        first = self._call(
            "Plan",
            "sss",
            ("recovery", "manifest-r1", "bus-idem-1"),
        )
        replay = self._call(
            "Plan",
            "sss",
            ("recovery", "manifest-r1", "bus-idem-1"),
        )
        self.assertEqual(str(first[0]), str(replay[0]))
        self.assertEqual(str(first[1]), str(replay[1]))
        self.assert_wire_error(
            wire.IDEMPOTENCY_CONFLICT_ERROR,
            "Plan",
            "sss",
            ("recovery", "manifest-r2", "bus-idem-1"),
        )

    def test_approval_get_fails_closed_without_authority(self) -> None:
        error = self.assert_wire_error(
            wire.APPROVAL_UNAVAILABLE_ERROR,
            "ApprovalGet",
            "s",
            ("approval-anything",),
        )
        self.assertEqual(
            error.get_dbus_message(),
            "approval authority is not available",
        )

    def test_approval_source_refused_for_non_approval_uid(self) -> None:
        # In-code uid restriction (N7) proven on a PERMISSIVE bus: bus
        # policy is not the perimeter (gate 10).
        for method, signature, args in (
            ("PlanMetadata", "s", (self.state["seat_b_plan_handle"],)),
            (
                "PlanSummary",
                "ss",
                (
                    self.state["seat_b_plan_handle"],
                    self.state["seat_b_plan_digest"],
                ),
            ),
        ):
            error = self.assert_wire_error(
                wire.POLICY_DENIED_ERROR,
                method,
                signature,
                args,
                wire.APPROVAL_SOURCE_INTERFACE,
            )
            self.assertEqual(error.get_dbus_message(), "subject_denied")


@requires_private_bus
class UnregisteredUidBusTests(PrivateBusCase):
    """Gate 9 + 10: the registry does NOT contain the calling uid; the
    bus is permissive; every method must refuse with the exact
    authorization error name."""

    REGISTER_SUBJECT = False

    def test_every_method_refuses_an_unregistered_uid(self) -> None:
        calls = (
            ("Capabilities", "", (), wire.MANAGER_INTERFACE),
            (
                "Plan",
                "sss",
                ("recovery", "manifest-r1", "unreg-1"),
                wire.MANAGER_INTERFACE,
            ),
            (
                "Apply",
                "ssss",
                ("a" * 64, "b" * 64, "unreg-2", "approval"),
                wire.MANAGER_INTERFACE,
            ),
            (
                "Verify",
                "sss",
                ("recovery", "a" * 64, "unreg-3"),
                wire.MANAGER_INTERFACE,
            ),
            (
                "Rollback",
                "sss",
                ("a" * 64, "unreg-4", "approval"),
                wire.MANAGER_INTERFACE,
            ),
            ("OperationGet", "s", ("a" * 64,), wire.MANAGER_INTERFACE),
            ("ApprovalGet", "s", ("approval",), wire.MANAGER_INTERFACE),
            (
                "PlanMetadata",
                "s",
                ("a" * 64,),
                wire.APPROVAL_SOURCE_INTERFACE,
            ),
            (
                "PlanSummary",
                "ss",
                ("a" * 64, "b" * 64),
                wire.APPROVAL_SOURCE_INTERFACE,
            ),
        )
        for method, signature, args, interface in calls:
            error = self.assert_wire_error(
                wire.POLICY_DENIED_ERROR,
                method,
                signature,
                args,
                interface,
            )
            self.assertEqual(
                error.get_dbus_message(),
                "subject_denied",
                method,
            )


@requires_private_bus
class ApprovalSourceBusTests(PrivateBusCase):
    """The configured approval uid calling the restricted interface."""

    APPROVAL_UID_IS_CALLER = True

    def test_plan_metadata_for_another_subjects_plan(self) -> None:
        metadata = self._call(
            "PlanMetadata",
            "s",
            (self.state["seat_b_plan_handle"],),
            wire.APPROVAL_SOURCE_INTERFACE,
        )
        values = tuple(str(item) for item in metadata)
        self.assertEqual(len(values), 8)
        self.assertEqual(values[0], "bravo")  # owner
        self.assertEqual(values[1], "bravo")  # performer == owner
        self.assertEqual(values[2], "recovery")
        self.assertEqual(values[3], "persona.install")
        self.assertEqual(values[4], self.state["seat_b_plan_digest"])

    def test_plan_summary_round_trip(self) -> None:
        digest, summary = self._call(
            "PlanSummary",
            "ss",
            (
                self.state["seat_b_plan_handle"],
                self.state["seat_b_plan_digest"],
            ),
            wire.APPROVAL_SOURCE_INTERFACE,
        )
        self.assertEqual(str(digest), self.state["seat_b_plan_digest"])
        raw = json.loads(str(summary))
        self.assertEqual(raw["schema"], "pantheon-host-plan-summary/v1")
        self.assertEqual(raw["plan_handle"], self.state["seat_b_plan_handle"])

    def test_unknown_handle_still_unknown_for_approval_uid(self) -> None:
        self.assert_wire_error(
            wire.UNKNOWN_HANDLE_ERROR,
            "PlanMetadata",
            "s",
            ("e" * 64,),
            wire.APPROVAL_SOURCE_INTERFACE,
        )


DIST = HOST_CONTROL / "dist"

MANAGER_MEMBERS = frozenset(
    (
        "Capabilities",
        "Plan",
        "Apply",
        "Verify",
        "Rollback",
        "OperationGet",
        "ApprovalGet",
    )
)
APPROVAL_SOURCE_MEMBERS = frozenset(("PlanMetadata", "PlanSummary"))
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
BROKER_HEALTH_PATH = "/org/pantheon/HostBroker1/Health"


def _policies(tree: ElementTree.Element) -> dict:
    result: dict = {}
    for node in tree.findall("policy"):
        key = (
            ("context", node.get("context"))
            if node.get("context")
            else ("user", node.get("user"))
        )
        result[key] = node
    return result


def _assert_member_level_allows(
    case: unittest.TestCase,
    policy: ElementTree.Element,
    *,
    interface: str,
    members: frozenset,
) -> None:
    """Every allow is destination+interface+member exact; the member set
    equals the ratified inventory; nothing else is allowed."""

    allows = policy.findall("allow")
    case.assertEqual(len(policy.findall("deny")), 0)
    observed = set()
    for allow in allows:
        case.assertIsNone(allow.get("own"), "no own grant on a caller policy")
        case.assertEqual(
            allow.get("send_destination"),
            "org.pantheon.HostBroker1",
        )
        case.assertEqual(allow.get("send_interface"), interface)
        member = allow.get("send_member")
        case.assertIsNotNone(
            member,
            "an allow without send_member is interface-wide — drift "
            "toward permissiveness (N8)",
        )
        observed.add(member)
    case.assertEqual(observed, set(members))


def _assert_broker_monitor_health_policy(
    case: unittest.TestCase,
    policy: ElementTree.Element,
) -> None:
    case.assertEqual(len(policy.findall("deny")), 0)
    observed = {
        tuple(sorted(allow.attrib.items()))
        for allow in policy.findall("allow")
    }
    case.assertEqual(
        observed,
        {
            tuple(
                sorted(
                    {
                        "send_destination": "org.pantheon.HostBroker1",
                        "send_path": BROKER_HEALTH_PATH,
                        "send_interface": PROPERTIES_INTERFACE,
                        "send_member": "Get",
                    }.items()
                )
            ),
            tuple(
                sorted(
                    {
                        "send_destination": "org.pantheon.HostBroker1",
                        "send_path": BROKER_HEALTH_PATH,
                        "send_interface": PROPERTIES_INTERFACE,
                        "send_member": "GetAll",
                    }.items()
                )
            ),
            tuple(
                sorted(
                    {
                        "receive_sender": "org.pantheon.HostBroker1",
                        "receive_path": BROKER_HEALTH_PATH,
                        "receive_interface": PROPERTIES_INTERFACE,
                        "receive_member": "PropertiesChanged",
                        "receive_type": "signal",
                    }.items()
                )
            ),
        },
    )


class BusPolicyArtifactTests(unittest.TestCase):
    """The staged bus policy cannot silently drift permissive (N8)."""

    def test_base_policy_is_default_deny_with_exact_allowances(self) -> None:
        tree = ElementTree.parse(DIST / "org.pantheon.HostBroker1.conf")
        policies = _policies(tree.getroot())
        self.assertEqual(
            set(policies),
            {
                ("context", "default"),
                ("user", "pantheon-hostbroker"),
                ("user", "pantheon-hostmonitor"),
                ("user", "pantheon-hostapproval"),
            },
        )
        # deny-by-default: the default context contains ONLY denies
        default = policies[("context", "default")]
        self.assertEqual(default.findall("allow"), [])
        denies = {
            tuple(sorted(deny.attrib.items()))
            for deny in default.findall("deny")
        }
        self.assertEqual(
            denies,
            {
                (("own", "org.pantheon.HostBroker1"),),
                (("send_destination", "org.pantheon.HostBroker1"),),
                (
                    ("receive_sender", "org.pantheon.HostBroker1"),
                    ("receive_type", "signal"),
                ),
            },
        )
        # the broker account may own the name and nothing else
        broker = policies[("user", "pantheon-hostbroker")]
        self.assertEqual(len(broker.findall("deny")), 0)
        broker_allows = broker.findall("allow")
        self.assertEqual(len(broker_allows), 1)
        self.assertEqual(
            broker_allows[0].get("own"),
            "org.pantheon.HostBroker1",
        )
        self.assertIsNone(broker_allows[0].get("send_destination"))
        # the approval account gets exactly the two ApprovalSource
        # members, member-level
        _assert_member_level_allows(
            self,
            policies[("user", "pantheon-hostapproval")],
            interface="org.pantheon.HostBroker1.ApprovalSource",
            members=APPROVAL_SOURCE_MEMBERS,
        )
        _assert_broker_monitor_health_policy(
            self,
            policies[("user", "pantheon-hostmonitor")],
        )

    def test_seat_template_allows_exactly_the_seven_manager_members(
        self,
    ) -> None:
        raw = (
            DIST / "pantheon-host-broker-seat.conf.template"
        ).read_text(encoding="utf-8")
        self.assertIn("@SEAT_ACCOUNT@", raw)
        substituted = raw.replace("@SEAT_ACCOUNT@", "testseat")
        tree = ElementTree.fromstring(substituted)
        policies = _policies(tree)
        self.assertEqual(set(policies), {("user", "testseat")})
        _assert_member_level_allows(
            self,
            policies[("user", "testseat")],
            interface="org.pantheon.HostBroker1.Manager",
            members=MANAGER_MEMBERS,
        )

    def test_no_wildcards_anywhere_in_staged_policies(self) -> None:
        for name in (
            "org.pantheon.HostBroker1.conf",
            "pantheon-host-broker-seat.conf.template",
        ):
            raw = (DIST / name).read_text(encoding="utf-8")
            self.assertNotIn('="*"', raw, name)
            self.assertNotIn("eavesdrop", raw, name)


class SystemdUnitArtifactTests(unittest.TestCase):
    def _unit(self) -> dict:
        raw = (DIST / "pantheon-host-broker.service").read_text(
            encoding="utf-8"
        )
        values: dict = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "[", ";")):
                continue
            key, _, value = line.partition("=")
            values.setdefault(key, []).append(value)
        return values

    def test_unit_is_type_dbus_unprivileged_and_hardened(self) -> None:
        unit = self._unit()
        self.assertEqual(unit["Type"], ["dbus"])
        self.assertEqual(unit["BusName"], ["org.pantheon.HostBroker1"])
        self.assertEqual(unit["User"], ["pantheon-hostbroker"])
        self.assertNotEqual(unit["User"], ["root"])
        self.assertEqual(unit["NoNewPrivileges"], ["yes"])
        self.assertEqual(unit["ProtectSystem"], ["strict"])
        self.assertEqual(unit["ProtectHome"], ["yes"])
        self.assertEqual(unit["PrivateTmp"], ["yes"])
        self.assertEqual(unit["RestrictAddressFamilies"], ["AF_UNIX"])
        # empty bounding set: the broker holds no capabilities at all
        self.assertEqual(unit["CapabilityBoundingSet"], [""])
        self.assertEqual(unit["SystemCallFilter"], ["@system-service"])
        exec_start = unit["ExecStart"]
        self.assertEqual(len(exec_start), 1)
        self.assertTrue(exec_start[0].startswith("/"))
        for metachar in ("|", ";", "&", "$", "`"):
            self.assertNotIn(metachar, exec_start[0])

    def test_unit_is_labeled_staged_and_provisional(self) -> None:
        raw = (DIST / "pantheon-host-broker.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("STAGED ARTIFACT", raw)
        self.assertIn("PROVISIONAL", raw)
        self.assertIn("T-2920", raw)


if __name__ == "__main__":  # must stay LAST (count-stability, T-2170)
    unittest.main()
