"""ORG-071 admission seam: how PassportHandshake consults load gates (T-2857 PR2).

A gate is anything with `check(ctx)` and `release(ctx)`. PassportHandshake
takes an ordered sequence of them (`admission=`, default none) and consults
every gate at each phase below, in order; the first breach ABORTs the
handshake through _breach() (internal reason + one audit event; the peer sees
only PEER_ABORT). With no gates supplied the handshake behaves exactly as
before.

Phases, in the order one run() reaches them:

  START     before any work: handshake rates, concurrency caps, the
            pre-auth (or unknown-source) budget. The rate limiter.
  RESOLVE   immediately before the outbound DNS/TXT resolution: the
            amplification bounds. The rate limiter.
  PRESENT   before the scheme adapter's verify(), with `scheme` set: the
            scheme's weighted verify cost (ORG-071 D5). The rate limiter.
  POST_AUTH after a verified presentation, before admit, with `principal`
            set to the verified DID. The rate limiter ignores it; the D4 quota
            gate (netcaps_quota.QuotaGate) acts only here, charging the
            verified DID's quota. It runs after the limiter in the same
            sequence: `admission=[limiter, quota]`.

Release contract: `release(ctx)` is called exactly once, with the START
context, for every gate whose START check passed, when run() ends by any path
(admit, ABORT, or an exception). A gate that refused START, or that START
never reached, is not released. Gates that hold no in-flight state make it a
no-op.

This module imports only netcaps_breach, so enforcement modules can import it
without a cycle through passport_handshake. `scheme` is the Scheme NAME (a
string) for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from tools.a2a.netcaps_breach import NetcapsBreach


class AdmissionPhase(Enum):
    START = "handshake_start"
    RESOLVE = "resolve"
    PRESENT = "present"
    POST_AUTH = "post_auth"


@dataclass(frozen=True)
class AdmissionContext:
    """What a gate may key on. `source` is the transport identity (IP/subnet)
    or None when the caller has none, which gates treat as the single
    unknown-source key (fail-closed). `known` is True when the claimed DID is
    in the local allowlist."""

    phase: AdmissionPhase
    endpoint_id: str
    source: str | None
    known: bool
    scheme: str | None = None      # PRESENT onward
    principal: str | None = None   # POST_AUTH: the verified DID
    resource: str = "txt"          # RESOLVE: what is fetched ("txt" | "did_doc")


@runtime_checkable
class AdmissionGate(Protocol):
    def check(self, ctx: AdmissionContext) -> NetcapsBreach | None: ...

    def release(self, ctx: AdmissionContext) -> None: ...
