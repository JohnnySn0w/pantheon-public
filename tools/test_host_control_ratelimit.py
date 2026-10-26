"""ORG-052 Unit 5a (T-2917 ruling i1): broker-side per-uid rate limiting.

Pure bucket semantics under an injected clock, plus the transport-boundary
ordering contract on the real handler code: identity FIRST, then the
budget, then the one domain call; a refused call never reaches the domain.
"""

from __future__ import annotations

import unittest

from tools.host_control import wire
from tools.host_control.contracts import HostControlError, RateLimited
from tools.host_control.ratelimit import (
    DEFAULT_MUTATING,
    DEFAULT_READ,
    METHOD_BUDGETS,
    BrokerRateLimits,
    BucketSpec,
    TokenBucketLimiter,
)
from tools.test_host_control_dbus import (
    FakeRegistryResolves,
    FakeRegistryUnregistered,
    _bare_service,
    dbus_service,
    requires_dbus,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TokenBucketTests(unittest.TestCase):
    def test_burst_then_refusal_then_refill(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(BucketSpec(capacity=3, refill_per_second=1.0), clock=clock)
        self.assertEqual([limiter.allow(7) for _ in range(4)], [True, True, True, False])
        clock.now += 1.0
        self.assertTrue(limiter.allow(7))
        self.assertFalse(limiter.allow(7))

    def test_refill_never_exceeds_capacity(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(BucketSpec(capacity=2, refill_per_second=1.0), clock=clock)
        limiter.allow(7)
        clock.now += 3600.0
        self.assertEqual([limiter.allow(7) for _ in range(3)], [True, True, False])

    def test_buckets_are_per_key(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(BucketSpec(capacity=1, refill_per_second=0.001), clock=clock)
        self.assertTrue(limiter.allow(1003))
        self.assertFalse(limiter.allow(1003))
        self.assertTrue(limiter.allow(1004))

    def test_memory_is_bounded_and_eviction_is_never_stricter(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(
            BucketSpec(capacity=1, refill_per_second=0.001), clock=clock, max_tracked=2
        )
        self.assertTrue(limiter.allow(1))
        self.assertTrue(limiter.allow(2))
        self.assertTrue(limiter.allow(3))  # evicts uid 1 (least recent)
        self.assertEqual(limiter.tracked(), 2)
        self.assertTrue(limiter.allow(1))  # a fresh bucket starts full

    def test_clock_going_backwards_grants_nothing_extra(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketLimiter(BucketSpec(capacity=1, refill_per_second=1.0), clock=clock)
        self.assertTrue(limiter.allow(7))
        clock.now -= 500.0
        self.assertFalse(limiter.allow(7))

    def test_invalid_specs_refuse(self) -> None:
        for capacity, refill in ((0, 1.0), (1, 0.0), (1, -1.0)):
            with self.subTest(capacity=capacity, refill=refill):
                with self.assertRaises(HostControlError):
                    BucketSpec(capacity=capacity, refill_per_second=refill)


class BrokerRateLimitsTests(unittest.TestCase):
    def test_method_budget_table_is_exactly_the_seven_manager_methods(self) -> None:
        self.assertEqual(
            METHOD_BUDGETS,
            {
                "Plan": "mutating",
                "Apply": "mutating",
                "Verify": "mutating",
                "Rollback": "mutating",
                "Capabilities": "read",
                "OperationGet": "read",
                "ApprovalGet": "read",
            },
        )
        self.assertLess(DEFAULT_MUTATING.capacity, DEFAULT_READ.capacity)

    def test_mutating_and_read_budgets_are_independent(self) -> None:
        clock = FakeClock()
        limits = BrokerRateLimits(
            mutating=BucketSpec(capacity=1, refill_per_second=0.001),
            read=BucketSpec(capacity=1, refill_per_second=0.001),
            clock=clock,
        )
        limits.check(1003, "Plan")
        with self.assertRaises(RateLimited):
            limits.check(1003, "Apply")
        limits.check(1003, "OperationGet")  # read budget untouched
        with self.assertRaises(RateLimited):
            limits.check(1003, "ApprovalGet")
        limits.check(1004, "Rollback")  # another uid untouched

    def test_unknown_method_and_non_int_uid_fail_loudly(self) -> None:
        limits = BrokerRateLimits()
        with self.assertRaises(HostControlError):
            limits.check(1003, "Execute")
        for bad in ("1003", True, None):
            with self.subTest(uid=bad):
                with self.assertRaises(HostControlError):
                    limits.check(bad, "Plan")  # type: ignore[arg-type]

    def test_rate_limited_maps_to_its_provisional_wire_name(self) -> None:
        exc = RateLimited("mutating request budget exhausted; retry later")
        self.assertEqual(wire.error_name_for(exc), wire.RATE_LIMITED_ERROR)
        self.assertEqual(wire.RATE_LIMITED_ERROR, "org.pantheon.HostBroker1.Error.RateLimited")
        self.assertIn(wire.RATE_LIMITED_ERROR, wire.CLOSED_ERROR_NAMES)
        self.assertLessEqual(len(wire.error_body_for(exc)), 200)


class RecordingPlanService:
    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, uid: int):
        self.calls += 1
        raise HostControlError("domain reached")


@requires_dbus
class HandlerOrderingTests(unittest.TestCase):
    """Identity first, then budget, then domain (ruling i1)."""

    def _exhausted(self) -> BrokerRateLimits:
        limits = BrokerRateLimits(
            mutating=BucketSpec(capacity=1, refill_per_second=0.001),
            read=BucketSpec(capacity=1, refill_per_second=0.001),
            clock=FakeClock(),
        )
        limits.check(1003, "Capabilities")
        limits.check(1003, "Plan")
        return limits

    def test_over_budget_caller_is_refused_before_the_domain(self) -> None:
        import dbus as dbus_module

        plan_service = RecordingPlanService()
        instance = _bare_service(
            _registry=FakeRegistryResolves(),
            _plan_service=plan_service,
            _rate_limits=self._exhausted(),
        )
        instance._get_unix_user = lambda sender: 1003
        with self.assertRaises(dbus_module.DBusException) as caught:
            dbus_service.HostBroker1Object.Capabilities(instance, sender=":1.9")
        self.assertEqual(caught.exception.get_dbus_name(), wire.RATE_LIMITED_ERROR)
        self.assertEqual(plan_service.calls, 0)

    def test_every_manager_method_is_rate_limited(self) -> None:
        import dbus as dbus_module

        args = {
            "Capabilities": (),
            "Plan": ("recovery", "manifest-r1", "k"),
            "Apply": ("a" * 64, "b" * 64, "k", "c" * 64),
            "Verify": ("recovery", "a" * 64, "k"),
            "Rollback": ("a" * 64, "k", "c" * 64),
            "OperationGet": ("a" * 64,),
            "ApprovalGet": ("c" * 64,),
        }
        self.assertEqual(set(args), set(METHOD_BUDGETS))
        for method, call_args in args.items():
            with self.subTest(method=method):
                instance = _bare_service(
                    _registry=FakeRegistryResolves(),
                    _rate_limits=self._exhausted(),
                )
                instance._get_unix_user = lambda sender: 1003
                with self.assertRaises(dbus_module.DBusException) as caught:
                    getattr(dbus_service.HostBroker1Object, method)(
                        instance, *call_args, sender=":1.9"
                    )
                self.assertEqual(caught.exception.get_dbus_name(), wire.RATE_LIMITED_ERROR)

    def test_unregistered_uid_gets_authorization_refusal_not_rate_limited(self) -> None:
        import dbus as dbus_module

        instance = _bare_service(
            _registry=FakeRegistryUnregistered(),
            _rate_limits=self._exhausted(),
        )
        instance._get_unix_user = lambda sender: 1003
        with self.assertRaises(dbus_module.DBusException) as caught:
            dbus_service.HostBroker1Object.Plan(instance, "recovery", "m", "k", sender=":1.9")
        self.assertEqual(caught.exception.get_dbus_name(), wire.POLICY_DENIED_ERROR)
        self.assertEqual(caught.exception.get_dbus_message(), "subject_denied")

    def test_init_always_installs_a_limiter(self) -> None:
        import inspect

        source = inspect.getsource(dbus_service.HostBroker1Object.__init__)
        self.assertIn("BrokerRateLimits()", source)


if __name__ == "__main__":
    unittest.main()
