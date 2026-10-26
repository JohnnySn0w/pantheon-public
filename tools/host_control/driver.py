"""The broker-internal operation driver (T-2917 5b; ORG-052 sections 5-6).

authorize -> dispatch -> ExecutionGet collect -> append_result, with
recovery of ``pending_operations()`` at broker start.

Thread model (the call-back deadlock constraint): HostApproval1's
``ValidateAndBind`` calls back the broker's ``PlanMetadata``, and the
executor calls back ``OperationSpec``. Both are served on the broker's GLib
main loop, so the driver must never block that loop. It runs on ONE
dedicated thread that owns its OWN ``BrokerJournal`` handle (the sqlite
connection is thread-bound; cross-connection safety is the journal's
BEGIN IMMEDIATE + busy timeout), its own ``OperationService``, and its own
blocking bus connections for the approval and executor clients. D-Bus
handlers only ``submit()`` handles onto a thread-safe queue.

Recovery rules (ruling B11), all fail closed and visible:

* ``prepared``/``authorized`` resume; a plan holding a class this
  revision does not execute is left untouched (never authorized, so no
  grant is ever bound) - ruling B4;
* ``rollback`` operations are driven like apply since T-2917 5d.2 (ruling
  T-2917:D15): bound, dispatched, collected, completed;
* with ``DriverContext.compensate`` (T-4964, off by default), an apply whose
  COLLECTED result failed with restorable mutations gets a broker-created
  compensation (a rollback inheriting the apply's authority, no second
  grant), created atomically with the failed receipt and driven next; a
  lost result parked in ``manual_recovery`` is never compensated;
* a verify in ``dispatch_intent``/``dispatched`` is re-dispatched (it is
  read-only) when its execution is unknown, bounded per drive AND over the
  operation's lifetime (T-2917 5d.1, ruling T-2917:D18): once
  ``LIFETIME_VERIFY_REDISPATCH_CAP`` re-dispatches are journaled, the
  verify ends terminally ``failed`` with no mutations (honest: it is
  read-only);
* an execution the executor REFUSED before its engine ran (state
  ``refused``, T-2917:D14) is classified from the journal: a verify, or an
  operation whose spec was never served or was served after the latest
  pre-Execute marker, ends ``failed`` with no mutations (nothing ran);
  otherwise the serve itself was refused, the earlier attempt's outcome is
  unknown, and the operation is left for the reconciler, never recorded
  as a clean failure;
* an apply in ``dispatch_intent`` is completed only when the earlier
  hand-off was DEFINITIVELY refused in this run; an apply in
  ``dispatched`` is collected through its recorded execution reference,
  and when the executor no longer holds it the operation STAYS
  ``dispatched`` (visible through OperationGet) for the reconciler. It
  is never re-dispatched without proof.

Live-state reconciliation (T-2917 5d.2) is wired per driver context
(``DriverContext.reconcile``; the production context sets it, with a
``BrokerLiveStateReconciler`` in its OperationService). With it, a
mutating operation whose result is lost is no longer a dead end:

* left in ``dispatch_intent`` (the Execute reply was lost): the IDENTICAL
  Execute is re-issued (E29 P4); the executor deduplicates it to the
  execution it holds, or, if it restarted, its fetch of the already-served
  spec is refused and the refusal is reconciled;
* ``dispatched`` with an unknown execution, or a refusal of an already-
  served spec: ``OperationService.recover`` runs the reconciler, which
  re-dispatches only with proof (a never-served arm, or a probe-clean apply
  or a rollback through a journaled, capped re-arm) and otherwise parks the
  operation loudly in ``manual_recovery``.

Health (T-2917 5d.4, ruling T-2917:D21; HO ruling on the roster): the
driver thread keeps a lock-guarded ``DriverHealthState`` cache. It refreshes
the reachability legs (``DriverContext.reachability``) after each drive and
every ``HEALTH_REFRESH_SECONDS`` while idle (``queue.get(timeout=)``), with
its OWN blocking connection. The three Health1 probes built here
(``operation_driver_probe``, ``executor_reachability_probe``,
``approval_reachability_probe``) only READ that cache and the driver's
flags, so a Health1 ``Get`` on the broker's main loop never blocks on the
bus. ``operation_driver``: unhealthy when the driver failed (setup failure,
dead thread), degraded until it has recovered, ready otherwise.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Final, Protocol

from tools.host_control.contracts import (
    TERMINAL_OPERATION_STATES,
    ApprovalUnavailable,
    HostControlError,
    OperationRecord,
    RecoveryRefused,
)
from tools.host_control.health import HealthProbe, HealthProbeResult
from tools.host_control.executor_client import (
    DEFINITIVE_DISPATCH_FAILURES,
    CollectedResult,
    ExecutorAmbiguous,
    ExecutorUnknownExecution,
)
from tools.host_control.journal import BrokerJournal
from tools.host_control.operations import ExecutorPort, OperationService
from tools.host_control.reconciler import ReconcilePending

DEFAULT_POLL_SCHEDULE: Final = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0)
# T-2917 5c.2: larger than the executor's own run cap
# (executor/wire.py EXECUTOR_RUN_BUDGET_SECONDS, 3300 s), so a collect never
# gives up on an execution that is still within its budget.
DEFAULT_COLLECT_TIMEOUT: Final = 3600.0
DEFAULT_MAX_ATTEMPTS: Final = 5
DEFAULT_MAX_VERIFY_REDISPATCHES: Final = 3
# T-2917 5d.1 (carry-in N5a, ruling T-2917:D18): journaled re-dispatches
# of one verify over its whole lifetime; at the cap it ends failed.
LIFETIME_VERIFY_REDISPATCH_CAP: Final = 3
VERIFY_CAP_DETAIL: Final = "verify re-dispatch cap reached; execution lost"
# T-2917 5d.4: the idle refresh period of the driver's health cache, and
# the revision label its legs report (PROVISIONAL)
HEALTH_REFRESH_SECONDS: Final = 30.0
HEALTH_REVISION: Final = "T-2917-5d"
# a cached reachability observation older than this is reported stale
# (degraded), never as its old verdict (MR-2 review, LOW)
HEALTH_STALE_FACTOR: Final = 3
_HEALTH_REASON_LIMIT: Final = 200
_LOG = logging.getLogger("tools.host_control.driver")
RETRYABLE_OUTCOMES: Final = frozenset(
    (
        "approval_unavailable",
        "executor_unavailable",
        "dispatch_refused",
        "collect_pending",
        "reconcile_pending",
    )
)


class ReachabilitySource(Protocol):
    """Driver-thread reachability observations (T-2917 5d.4); each call may
    block on the bus, so ONLY the driver thread calls them."""

    def executor_reachability(self) -> HealthProbeResult: ...

    def approval_reachability(self) -> HealthProbeResult: ...


def _health_text(value: object) -> str:
    """Bounded printable-ASCII health reason text."""

    try:
        text = str(value)
    except Exception:
        text = "unprintable reason"
    cleaned = "".join(ch if 32 <= ord(ch) <= 126 else "?" for ch in text)
    return cleaned[:_HEALTH_REASON_LIMIT] or "unspecified"


class DriverHealthState:
    """Lock-guarded health cache: WRITTEN by the driver thread, READ by the
    Health1 probes (which never touch the bus)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wired: bool | None = None  # None: never refreshed
        self._executor: HealthProbeResult | None = None
        self._approval: HealthProbeResult | None = None
        self._observed_at: float | None = None
        self.refreshes = 0

    def record(
        self,
        *,
        wired: bool,
        executor: HealthProbeResult | None = None,
        approval: HealthProbeResult | None = None,
        observed_at: float | None = None,
    ) -> None:
        with self._lock:
            self._observed_at = observed_at
            self._wired = wired
            self._executor = executor
            self._approval = approval
            self.refreshes += 1

    def read(self) -> tuple[bool | None, HealthProbeResult | None, HealthProbeResult | None]:
        with self._lock:
            return self._wired, self._executor, self._approval

    def observed_at(self) -> float | None:
        """The driver clock reading (monotonic) of the last observation."""

        with self._lock:
            return self._observed_at


class CollectingPort(ExecutorPort, Protocol):
    def ready(self, operation: OperationRecord) -> bool: ...

    def collect(self, operation: OperationRecord, execution_ref: str) -> CollectedResult | None: ...


@dataclass
class DriverContext:
    service: OperationService
    journal: BrokerJournal
    port: CollectingPort
    close: Callable[[], None]
    # T-2917 5d.2: route lost mutating results to the service's live-state
    # reconciler (the production context sets it with a
    # BrokerLiveStateReconciler); False keeps the 5c dead end
    # ``awaiting_reconciler`` for contexts without one
    reconcile: bool = False
    # T-2917 5d.4: the reachability legs' source (driver-thread only);
    # None leaves those Health1 legs ``not_configured``
    reachability: ReachabilitySource | None = None
    # T-4964 (ORG-052 section 5 "may"): automatic broker compensation of a
    # FAILED apply whose receipt holds restorable mutations. Off by default
    # (inert until the T-2920 production entrypoint opts in); it requires
    # ``reconcile``, because a compensation whose result is lost needs the
    # reconciler's capped re-arm.
    compensate: bool = False

    def __post_init__(self) -> None:
        if self.compensate and not self.reconcile:
            raise HostControlError("compensation requires the live-state reconciler")


class DriverCore:
    """Synchronous single-operation state walk (unit-testable with fakes)."""

    def __init__(
        self,
        context: DriverContext,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_schedule: tuple[float, ...] = DEFAULT_POLL_SCHEDULE,
        collect_timeout: float = DEFAULT_COLLECT_TIMEOUT,
        max_verify_redispatches: int = DEFAULT_MAX_VERIFY_REDISPATCHES,
        verify_lifetime_cap: int = LIFETIME_VERIFY_REDISPATCH_CAP,
    ) -> None:
        self._max_verify_redispatches = max_verify_redispatches
        self._verify_lifetime_cap = verify_lifetime_cap
        self._ctx = context
        self._sleep = sleeper
        self._clock = clock
        self._schedule = poll_schedule
        self._collect_timeout = collect_timeout
        self._refused_this_run: set[str] = set()
        # T-4964: compensations created during a drive, for the caller to
        # drive next (a prepared compensation is also recovered at start)
        self.followups: list[str] = []

    def drive(self, operation_handle: str) -> str:
        service, journal, port = self._ctx.service, self._ctx.journal, self._ctx.port
        record = journal.load_operation_unbound(operation_handle)
        if record.state in TERMINAL_OPERATION_STATES:
            return "terminal"
        if record.state in ("prepared", "authorized") and not service.revision_implements(record):
            return "unsupported_pending"
        if record.state == "prepared":
            try:
                record = service.authorize(operation_handle)
            except ApprovalUnavailable:
                return "approval_unavailable"
            if record.state == "refused":
                return "refused"
        if record.state == "authorized":
            if not port.ready(record):
                return "executor_unavailable"
            try:
                record = service.dispatch(operation_handle, port)
            except DEFINITIVE_DISPATCH_FAILURES:
                self._refused_this_run.add(operation_handle)
                return "dispatch_refused"
            except ExecutorAmbiguous:
                record = journal.load_operation_unbound(operation_handle)
        if record.state == "dispatch_intent":
            # Each definitive refusal licenses at most ONE further Execute:
            # consume it BEFORE calling out, so a failure after an accepted
            # Execute (e.g. the journal write) can never re-dispatch a
            # mutating operation (review M4, ruling B11).
            refused = operation_handle in self._refused_this_run
            self._refused_this_run.discard(operation_handle)
            if record.kind != "verify" and not refused:
                if not self._ctx.reconcile:
                    return "awaiting_reconciler"
                try:
                    record = service.retry_identical_dispatch(operation_handle, port)
                except DEFINITIVE_DISPATCH_FAILURES:
                    self._refused_this_run.add(operation_handle)
                    return "dispatch_refused"
                except ExecutorAmbiguous:
                    return "reconcile_pending"
                return self._collect(record)
            try:
                record = service.resume_dispatch(
                    operation_handle,
                    port,
                    definitive_refusal=refused,
                )
            except DEFINITIVE_DISPATCH_FAILURES:
                # a NEW definitive refusal: nothing reached the executor
                self._refused_this_run.add(operation_handle)
                return "dispatch_refused"
            except ExecutorAmbiguous:
                return "reconcile_pending" if self._ctx.reconcile else "awaiting_reconciler"
        if record.state == "dispatched":
            return self._collect(record)
        return "terminal" if record.state in TERMINAL_OPERATION_STATES else "pending"

    def _collect(self, record: OperationRecord, *, recover: bool = True) -> str:
        service, journal, port = self._ctx.service, self._ctx.journal, self._ctx.port
        handle = record.operation_handle
        reference = journal.dispatch_execution_ref(handle)
        if reference is None:
            return "awaiting_reconciler"
        deadline = self._clock() + self._collect_timeout
        step = 0
        redispatches = 0
        while True:
            try:
                collected = port.collect(record, reference)
            except ExecutorUnknownExecution:
                if record.kind != "verify":
                    # B11: never re-dispatched here; the reconciler decides
                    return self._lost(record, recover=recover)
                # read-only, so re-dispatch is safe, but BOUNDED (review M3):
                # a hard cap per drive, and the deadline and back-off below
                # still apply before the next attempt
                if redispatches >= self._max_verify_redispatches or self._clock() >= deadline:
                    return "collect_pending"
                # ... and over the operation's LIFETIME (T-2917:D18): the
                # journal, not this drive, counts every earlier re-dispatch
                if journal.count_events(handle, ("redispatched",)) >= self._verify_lifetime_cap:
                    return self._end_verify_at_cap(record)
                redispatches += 1
                service.resume_dispatch(handle, port)
                reference = journal.dispatch_execution_ref(handle)
                if reference is None:
                    return "awaiting_reconciler"
                self._sleep(self._schedule[min(step, len(self._schedule) - 1)])
                step += 1
                continue
            except DEFINITIVE_DISPATCH_FAILURES:
                return "collect_pending"
            except ExecutorAmbiguous:
                return "collect_pending"
            if collected is not None and collected.refused:
                return self._classify_refused(record, collected, recover=recover)
            if collected is not None:
                compensate = self._ctx.compensate and not collected.ok and record.kind == "apply"
                final = service.append_result(
                    handle,
                    ok=collected.ok,
                    mutations=collected.result.mutations,
                    evidence=collected.result.evidence,
                    detail=collected.result.detail,
                    compensate=compensate,
                )
                if compensate:
                    compensation = journal.compensation_ref(handle)
                    if compensation is not None:
                        self.followups.append(compensation[0])
                return final.state
            if self._clock() >= deadline:
                return "collect_pending"
            self._sleep(self._schedule[min(step, len(self._schedule) - 1)])
            step += 1


    def _end_verify_at_cap(self, record: OperationRecord) -> str:
        service, journal = self._ctx.service, self._ctx.journal
        journal.append_event(
            current=record,
            event_kind="verify_redispatch_cap",
            payload={"cap": self._verify_lifetime_cap, "read_only": True},
            created_at=service.now_iso(),
        )
        final = service.append_result(
            record.operation_handle,
            ok=False,
            mutations=(),
            evidence=(),
            detail=VERIFY_CAP_DETAIL,
        )
        return final.state

    def _classify_refused(self, record: OperationRecord, collected: CollectedResult, *, recover: bool = True) -> str:
        """T-2917:D14/D27: a pre-engine refusal ends ``failed`` with no
        mutations ONLY when the journal proves nothing ran: a verify (read-
        only), or a mutating operation with exactly ONE Execute issued in its
        whole lifetime whose spec was never served or was served after that
        Execute's marker. With more than one Execute ever issued, a serve
        after the latest marker may belong to an EARLIER, still-outstanding
        execution (it is not proof the refused one fetched), so every such
        refusal is lost: reconciled, or parked loudly when opted out."""

        service, journal = self._ctx.service, self._ctx.journal
        handle = record.operation_handle
        single_execute = journal.execute_issuance_count(handle) == 1
        never_served = journal.count_events(handle, ("spec_served",)) == 0
        if record.kind == "verify" or (
            single_execute
            and (never_served or journal.spec_served_after_latest_dispatch(handle))
        ):
            final = service.append_result(
                handle,
                ok=False,
                mutations=(),
                evidence=(),
                detail=collected.result.detail,
            )
            return final.state
        # the SERVE was refused: an earlier attempt holds the spec and its
        # outcome is unknown; never a clean failed receipt
        return self._lost(record, recover=recover)

    def _lost(self, record: OperationRecord, *, recover: bool = True) -> str:
        """A mutating operation's result is lost. Without a reconciler this
        is the 5c dead end; with one, the service reconciles (T-2917 5d.2)."""

        if not self._ctx.reconcile:
            return "awaiting_reconciler"
        if not recover:
            # already reconciled once in this drive: let the driver retry
            return "reconcile_pending"
        service, journal, port = self._ctx.service, self._ctx.journal, self._ctx.port
        handle = record.operation_handle
        try:
            outcome = service.recover(handle, executor=port)
        except ReconcilePending:
            return "reconcile_pending"
        except RecoveryRefused:
            current = journal.load_operation_unbound(handle)
            if current.state == "manual_recovery":
                return "manual_recovery"
            return "awaiting_reconciler"
        except (*DEFINITIVE_DISPATCH_FAILURES, ExecutorAmbiguous):
            # the re-dispatch did not (provably) reach an executor; any
            # journaled re-arm stays outstanding for the next attempt
            return "reconcile_pending"
        if outcome.action in ("dispatched", "redispatched"):
            return self._collect(outcome.record, recover=False)
        return outcome.record.state


_STOP = object()


class OperationDriver:
    """Owns the driver thread. ``context_factory`` runs IN that thread and
    builds the thread's own journal handle, service and executor port."""

    def __init__(
        self,
        *,
        context_factory: Callable[[], DriverContext],
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_schedule: tuple[float, ...] = DEFAULT_POLL_SCHEDULE,
        collect_timeout: float = DEFAULT_COLLECT_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff: float = 1.0,
        on_outcome: Callable[[str, str], None] | None = None,
        health_refresh: float = HEALTH_REFRESH_SECONDS,
    ) -> None:
        self._health_refresh = float(health_refresh)
        self.health = DriverHealthState()
        self._stopping = False
        self._context_factory = context_factory
        self._sleep = sleeper
        self._clock = clock
        self._schedule = poll_schedule
        self._collect_timeout = collect_timeout
        self._max_attempts = max_attempts
        self._retry_backoff = retry_backoff
        self._on_outcome = on_outcome
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self.recovered = threading.Event()
        self.idle = threading.Event()
        # set (bounded text) when the driver thread could not start or
        # died (review S3): Apply/Verify then refuse BEFORE prepare and
        # submit() refuses; surfaced through Health1 since 5d.4
        # (operation_driver_probe)
        self.failure: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise HostControlError("driver already started")
        self._thread = threading.Thread(target=self._run, name="host-broker-driver", daemon=True)
        self._thread.start()

    def submit(self, operation_handle: str) -> None:
        """Enqueue a durable operation. Refuses loudly when the driver is
        down: the PREPARED record stays durable and is recovered at the
        next broker start."""

        if self.failure is not None:
            raise HostControlError("operation driver unavailable; operation is recovered at broker restart")
        self.idle.clear()
        self._queue.put(str(operation_handle))

    @property
    def health_refresh(self) -> float:
        return self._health_refresh

    def clock(self) -> float:
        """The driver's monotonic clock seam (health cache age)."""

        return self._clock()

    @property
    def started(self) -> bool:
        return self._thread is not None

    def thread_dead(self) -> bool:
        """The driver thread exited without being asked to stop."""

        thread = self._thread
        return thread is not None and not thread.is_alive() and not self._stopping

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping = True
        self._queue.put(_STOP)
        if self._thread is not None:
            self._thread.join(timeout)

    def _report(self, handle: str, outcome: str) -> None:
        if self._on_outcome is not None:
            self._on_outcome(handle, outcome)

    def _drive_with_retries(self, core: DriverCore, handle: str) -> None:
        self._drive_one(core, handle)
        # T-4964: drive any compensation the drive created (a compensation is
        # a rollback, never itself compensated, so this does not chain)
        while core.followups:
            self._drive_one(core, core.followups.pop(0))

    def _drive_one(self, core: DriverCore, handle: str) -> None:
        for attempt in range(self._max_attempts):
            try:
                outcome = core.drive(handle)
            except Exception as exc:  # the driver thread must survive any one operation
                outcome = f"error:{type(exc).__name__}"
            self._report(handle, outcome)
            if outcome not in RETRYABLE_OUTCOMES or attempt + 1 == self._max_attempts:
                return
            self._sleep(self._retry_backoff * (attempt + 1))

    def _refresh_health(self, context: DriverContext) -> None:
        """Driver thread only: observe the reachability legs (may block on
        the bus) and publish them to the cache; never raises."""

        source = context.reachability
        if source is None:
            self.health.record(wired=False)
            return
        results = []
        for leg, observe in (
            ("executor", source.executor_reachability),
            ("approval", source.approval_reachability),
        ):
            try:
                result = observe()
                if not isinstance(result, HealthProbeResult):
                    raise TypeError("not a HealthProbeResult")
            except Exception as exc:
                result = HealthProbeResult(
                    status="degraded",
                    reason=_health_text(f"{leg} reachability probe failed: {type(exc).__name__}"),
                    revision=HEALTH_REVISION,
                )
            results.append(result)
        self.health.record(wired=True, executor=results[0], approval=results[1], observed_at=self._clock())

    def _run(self) -> None:
        try:
            context = self._context_factory()
        except Exception as exc:
            self.failure = f"driver setup failed: {type(exc).__name__}"[:200]
            _LOG.error("host broker operation driver: %s", self.failure)
            self.recovered.set()
            self.idle.set()
            return
        core = DriverCore(
            context,
            sleeper=self._sleep,
            clock=self._clock,
            poll_schedule=self._schedule,
            collect_timeout=self._collect_timeout,
        )
        try:
            self._refresh_health(context)
            for handle in context.journal.pending_operations():
                self._drive_with_retries(core, handle)
            self.recovered.set()
            while True:
                if self._queue.empty():
                    self.idle.set()
                try:
                    item = self._queue.get(timeout=self._health_refresh)
                except queue.Empty:
                    # idle: refresh the health cache (T-2917 5d.4)
                    self._refresh_health(context)
                    continue
                if item is _STOP:
                    return
                self._drive_with_retries(core, str(item))
                self._refresh_health(context)
        except Exception as exc:
            self.failure = f"driver thread died: {type(exc).__name__}"[:200]
            _LOG.error("host broker operation driver: %s", self.failure)
        finally:
            self.recovered.set()
            self.idle.set()
            context.close()


# ---------------------------------------------------------------------------
# Health1 probes over the driver's cache (T-2917 5d.4, ruling T-2917:D21)
# ---------------------------------------------------------------------------


def operation_driver_probe(driver: OperationDriver) -> HealthProbe:
    """``operation_driver`` (PROVISIONAL leg name, HO ruling): unhealthy
    when the driver failed, degraded while recovering, ready otherwise."""

    def probe() -> HealthProbeResult:
        failure = driver.failure
        if failure:
            return HealthProbeResult("unhealthy", _health_text(failure), HEALTH_REVISION)
        if driver.thread_dead():
            return HealthProbeResult("unhealthy", "driver thread is not running", HEALTH_REVISION)
        if not driver.started:
            return HealthProbeResult("degraded", "operation driver not started", HEALTH_REVISION)
        if not driver.recovered.is_set():
            return HealthProbeResult("degraded", "recovering pending operations", HEALTH_REVISION)
        return HealthProbeResult("ready", "", HEALTH_REVISION)

    return probe


def _cached_leg(driver: OperationDriver, leg: str) -> HealthProbe:
    def probe() -> HealthProbeResult:
        wired, executor, approval = driver.health.read()
        if wired is False:
            return HealthProbeResult(
                "not_configured", f"{leg} reachability source is not wired in this broker", HEALTH_REVISION
            )
        if driver.failure or driver.thread_dead():
            return HealthProbeResult(
                "degraded", f"{leg} reachability is stale: operation driver unavailable", HEALTH_REVISION
            )
        result = executor if leg == "executor" else approval
        if wired is None or result is None:
            return HealthProbeResult("degraded", f"{leg} reachability not yet observed", HEALTH_REVISION)
        observed = driver.health.observed_at()
        if observed is None or driver.clock() - observed > HEALTH_STALE_FACTOR * driver.health_refresh:
            return HealthProbeResult(
                "degraded", f"{leg} reachability is stale: last observation too old", HEALTH_REVISION
            )
        return result

    return probe


def executor_reachability_probe(driver: OperationDriver) -> HealthProbe:
    return _cached_leg(driver, "executor")


def approval_reachability_probe(driver: OperationDriver) -> HealthProbe:
    return _cached_leg(driver, "approval")
