"""ORG-071 passport network capabilities — the shared foundation (T-2857 PR1).

Ratified spec: architecture/adr/ORG-071-passport-network-capabilities.md, D1-D6
(ratified-with-trim, the operator TG 1784, 2026-09-06: the ADR keeps the parameter
NAMES as taxonomy; values are first-fire tuning at build, T-2857).

What lives here — the small, dependency-free base every ORG-071 enforcement
module (timeouts, rate limiter, resolution cache, quota enforcer, circuit
breaker) builds on:

  * `NetcapsParams` — the frozen, validated set of every named ORG-071
    parameter, grouped Caching / Timeouts / Connections / Budgets /
    Amplification / Sizes. `parameter_table()` renders the default-value table
    surfaced in PASSPORT.md (a test pins the two together).
  * The D6 vocabulary — `PEER_ABORT` (the single coarse peer-facing refusal),
    `AuditEventType` (the named ORG-070 event types), `AuditEvent`, and the
    injectable `AuditSink` protocol with `InMemoryAuditSink` (tests) and
    `LoggingAuditSink` (default until the ORG-070 store exists). The INTERNAL
    reasons are passport_handshake.AbortReason — extended, not duplicated.
  * `Clock` — the injectable monotonic clock every time-based component takes,
    with `MonotonicClock` (production) and `FakeClock` (tests never sleep).

PURE-LOCAL like the rest of tools/a2a: no network, no threads, no filesystem.
This module imports nothing from passport_handshake, so any later module can
import it without a cycle.

PROVISIONAL VALUES: every default below is a provisional first-fire default;
tuning is the operator's value-decision (ORG-071 D3, T-2857). The ADR deliberately
carries no numbers. The defaults lean conservative (a small federation of a few
hand-allowlisted kin seeds), so first fire errs toward refusing load, which is
the fail-closed direction (ORG-047 D1).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Protocol, runtime_checkable

logger = logging.getLogger("pantheon.a2a.netcaps")


# ---------------------------------------------------------------------------
# Named parameters (ORG-071 taxonomy; values per T-2857).
# ---------------------------------------------------------------------------

GROUPS = ("Caching", "Timeouts", "Connections", "Budgets", "Amplification", "Sizes")

# ORG-047 D5: "a rotation must propagate in minutes, not days". The positive
# DID-doc / status-list TTLs may never exceed this ceiling (ORG-071 D2: HTTP
# Cache-Control is an input clamped to the local max, never authority beyond
# it). 15 minutes is itself a provisional reading of "minutes".
ROTATION_CEILING_S = 900

# Budgets and the distinct-DID/anchor caps are counted per accounting window.
# The taxonomy names no window parameter, so it is a fixed constant here (PR2
# reads it); listed in the PASSPORT.md table footnote, not as a parameter.
ACCOUNTING_WINDOW_S = 60


def _p(default, group, unit, why, *, integer=False, zero_ok=False):
    return field(default=default, metadata={
        "group": group, "unit": unit, "why": why,
        "integer": integer, "zero_ok": zero_ok,
    })


@dataclass(frozen=True)
class NetcapsParams:
    """Every named ORG-071 bound, validated at construction.

    Provisional first-fire defaults; tuning is the operator's value-decision
    (ORG-071 D3, T-2857). Construct with keyword overrides; an invalid set
    raises ValueError (a programming/config error, so it fails fast at wiring
    time rather than degrading a live handshake).
    """

    # --- Caching (D2) ------------------------------------------------------
    did_doc_positive_ttl_max: float = _p(
        300, "Caching", "s",
        "positive DID-doc cache cap; well under the ORG-047 D5 rotation ceiling")
    status_list_positive_ttl_max: float = _p(
        300, "Caching", "s",
        "positive status-list cache cap; revocation freshness same as rotation")
    negative_ttl_definitive_absent: float = _p(
        60, "Caching", "s",
        "short class-TTL for 404/410/malformed/bad-sig, scoped to the DID")
    negative_ttl_transient_infra: float = _p(
        5, "Caching", "s",
        "brief advisory fetch-suppression only, never handshake denial")
    jitter_range_pct: float = _p(
        10, "Caching", "%",
        "refresh jitter against expiry stampedes; 0 disables, max 50",
        zero_ok=True)

    # --- Timeouts (D3) -----------------------------------------------------
    timeout_dns: float = _p(
        5, "Timeouts", "s", "DNS/TXT lookup; a cold DNSSEC-validating lookup fits")
    timeout_connect: float = _p(3, "Timeouts", "s", "TCP connect")
    timeout_tls: float = _p(3, "Timeouts", "s", "TLS handshake")
    timeout_fetch_read: float = _p(
        5, "Timeouts", "s", "per-fetch read (Agent Card, DID doc, status list)")
    timeout_handshake_roundtrip: float = _p(
        10, "Timeouts", "s", "one presentation request/response round-trip")
    timeout_total_handshake: float = _p(
        20, "Timeouts", "s",
        "wall-clock cap on the whole exchange; every step timeout must fit")

    # --- Connections (D1a) -------------------------------------------------
    max_conns_global: int = _p(
        64, "Connections", "conns", "open TCP/TLS connections, all sources",
        integer=True)
    max_conns_per_source: int = _p(
        4, "Connections", "conns", "open connections from one source",
        integer=True)
    max_concurrent_handshakes_global: int = _p(
        16, "Connections", "handshakes", "in-flight handshakes, all sources",
        integer=True)
    max_concurrent_handshakes_per_source: int = _p(
        4, "Connections", "handshakes",
        "in-flight handshakes from one source (a kin seed's personas share one IP)",
        integer=True)
    max_concurrent_handshakes_per_counterparty: int = _p(
        2, "Connections", "handshakes", "in-flight handshakes for one counterparty",
        integer=True)
    rate_handshake_per_source: float = _p(
        30, "Connections", "per min", "handshake starts from one source")
    rate_handshake_per_counterparty: float = _p(
        10, "Connections", "per min", "handshake starts for one counterparty")

    # --- Budgets (D1b; cost units per accounting window, weighted per D5) ---
    budget_preauth: float = _p(
        20, "Budgets", "units/window",
        "pre-auth work per source; the primary DoS target, so the tightest tier")
    budget_authenticated_ungranted: float = _p(
        50, "Budgets", "units/window", "verified but no ORG-069 grant yet")
    budget_granted: float = _p(
        200, "Budgets", "units/window", "default grant quota (D4) absent an override")
    budget_unknown_source: float = _p(
        100, "Budgets", "units/window",
        "isolated shared pool for all unknown sources, cannot starve known kin")
    budget_known_degraded: float = _p(
        20, "Budgets", "units/window", "known counterparty during its own outage")

    # --- Amplification (D1 outbound) ---------------------------------------
    resolution_rate_global: float = _p(
        120, "Amplification", "per min", "all outbound DID/status resolutions")
    resolution_rate_per_source_unresolved: float = _p(
        10, "Amplification", "per min",
        "uncached resolutions one source can trigger")
    resolution_rate_per_anchor: float = _p(
        30, "Amplification", "per min", "outbound fetches to one anchor host")
    max_distinct_dids_per_source_window: int = _p(
        16, "Amplification", "DIDs/window",
        "distinct DIDs one source may present (caching cannot bound these)",
        integer=True)
    max_distinct_anchors_per_source_window: int = _p(
        3, "Amplification", "hosts/window", "distinct anchor hosts one source may name",
        integer=True)
    circuit_breaker_threshold: int = _p(
        5, "Amplification", "failures", "consecutive transient failures that open it",
        integer=True)
    circuit_breaker_cooldown: float = _p(
        60, "Amplification", "s", "open time before the first recovery probe")
    circuit_breaker_probe_interval: float = _p(
        30, "Amplification", "s", "gap between recovery probes while still failing")

    # --- Sizes (D1c + response limits) -------------------------------------
    max_did_doc_bytes: int = _p(
        65536, "Sizes", "bytes", "a DID doc is a few KiB; 64 KiB is ample",
        integer=True)
    max_status_list_bytes: int = _p(
        65536, "Sizes", "bytes", "compressed StatusList2021 credential on the wire",
        integer=True)
    max_status_list_decompressed_bytes: int = _p(
        1048576, "Sizes", "bytes", "1 MiB bitstring = 8M entries; decompression-bomb cap",
        integer=True)
    max_compression_ratio: float = _p(
        1024, "Sizes", "x",
        "empty bitstrings compress about 350x, so the cap sits above that")
    max_negotiation_steps: int = _p(
        4, "Sizes", "steps", "message exchanges per handshake", integer=True)
    max_redirects: int = _p(
        0, "Sizes", "redirects", "did:web fetches follow none (SSRF/amplification)",
        integer=True, zero_ok=True)
    retry_policy: int = _p(
        0, "Sizes", "retries", "retries per fetch, transient-infra only; 0 = never",
        integer=True, zero_ok=True)

    def __post_init__(self) -> None:
        for f in fields(self):
            _check_value(f.name, getattr(self, f.name), f.metadata)
        self._check_cross_field()

    def _check_cross_field(self) -> None:
        if self.jitter_range_pct > 50:
            raise ValueError("jitter_range_pct must be <= 50")
        for name in ("did_doc_positive_ttl_max", "status_list_positive_ttl_max",
                     "negative_ttl_definitive_absent", "negative_ttl_transient_infra"):
            if getattr(self, name) > ROTATION_CEILING_S:
                raise ValueError(
                    f"{name} exceeds the ORG-047 D5 rotation ceiling "
                    f"({ROTATION_CEILING_S}s)")
        for name in ("timeout_dns", "timeout_connect", "timeout_tls",
                     "timeout_fetch_read", "timeout_handshake_roundtrip"):
            if getattr(self, name) > self.timeout_total_handshake:
                raise ValueError(f"{name} exceeds timeout_total_handshake")
        for part, whole in (
            ("max_conns_per_source", "max_conns_global"),
            ("max_concurrent_handshakes_per_source", "max_concurrent_handshakes_global"),
            ("max_concurrent_handshakes_per_counterparty",
             "max_concurrent_handshakes_global"),
            ("resolution_rate_per_source_unresolved", "resolution_rate_global"),
            ("resolution_rate_per_anchor", "resolution_rate_global"),
            ("budget_preauth", "budget_authenticated_ungranted"),
            ("budget_authenticated_ungranted", "budget_granted"),
            ("max_status_list_bytes", "max_status_list_decompressed_bytes"),
        ):
            if getattr(self, part) > getattr(self, whole):
                raise ValueError(f"{part} must be <= {whole}")
        if self.max_compression_ratio < 1:
            raise ValueError("max_compression_ratio must be >= 1")


def _check_value(name: str, value, meta) -> None:
    # bool is an int subclass; True is never a meaningful bound.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if meta["integer"] and not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if value < 0 or (value == 0 and not meta["zero_ok"]):
        floor = ">= 0" if meta["zero_ok"] else "> 0"
        raise ValueError(f"{name} must be {floor}, got {value!r}")


def parameter_table() -> str:
    """The default-value table (name, group, default, unit, rationale) as
    markdown — the surface handed to Orchestrator/the operator for first-fire tuning."""
    defaults = NetcapsParams()
    rows = ["| name | group | default | unit | rationale |",
            "|---|---|---|---|---|"]
    for f in fields(NetcapsParams):
        m = f.metadata
        rows.append(f"| `{f.name}` | {m['group']} | {getattr(defaults, f.name)} "
                    f"| {m['unit']} | {m['why']} |")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Clock (every time-based component takes one; tests never sleep).
# ---------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """Monotonic seconds. Only differences are meaningful — never wall time."""

    def monotonic(self) -> float: ...


class MonotonicClock:
    """Production clock: time.monotonic()."""

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Test clock that moves only when advanced (by a test or a stub
    dependency standing in for a slow network step)."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a monotonic clock cannot go backwards")
        self._now += seconds


# ---------------------------------------------------------------------------
# D6 vocabulary: coarse peer-facing abort + named ORG-070 audit events.
# ---------------------------------------------------------------------------

# The ONLY thing a peer learns about any ABORT (ORG-071 D6 anti-oracle): a
# rate-limit, quota, timeout or cache refusal reads exactly like an auth
# failure. The precise reason stays internal (AbortReason + the audit event).
PEER_ABORT = "handshake_refused"


class AuditEventType(Enum):
    """The named ORG-071 D6 event types emitted into the ORG-070 audit sink."""

    RATE_LIMIT_REJECTED = "rate_limit_rejected"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RESOLVER_TIMEOUT = "resolver_timeout"
    STATUS_FETCH_FAILED = "status_fetch_failed"
    NEGATIVE_CACHE_HIT = "negative_cache_hit"
    STALE_DENIED = "stale_denied"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    UNKNOWN_DID_BUDGET_EXCEEDED = "unknown_did_budget_exceeded"
    DID_RESOLUTION_FAILED = "did_resolution_failed"
    DOWNGRADE_STORE_FAILED = "downgrade_store_failed"  # T-5411


@dataclass(frozen=True)
class AuditEvent:
    """One ORG-071 audit record. `reason` is the internal AbortReason value (a
    string, so this module needs no import of passport_handshake); `at` is the
    emitting component's monotonic clock — the ORG-070 store adds wall time.
    `source` is the transport identity (IP/subnet) when the emitter has one."""

    type: AuditEventType
    endpoint_id: str
    reason: str
    detail: str
    at: float
    source: str | None = None


@runtime_checkable
class AuditSink(Protocol):
    """Where ORG-071 audit events go. The ORG-070 evidence store (Split E) will
    implement this; until then the default is LoggingAuditSink."""

    def emit(self, event: AuditEvent) -> None: ...


class InMemoryAuditSink:
    """Collects events in order, for tests."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)

    def of(self, event_type: AuditEventType) -> list[AuditEvent]:
        return [e for e in self.events if e.type is event_type]


class LoggingAuditSink:
    """Default sink: one WARNING log line per event."""

    def emit(self, event: AuditEvent) -> None:
        logger.warning("netcaps audit %s endpoint=%r source=%r reason=%s — %s",
                       event.type.value, event.endpoint_id, event.source,
                       event.reason, event.detail)
