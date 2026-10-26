# ORG-047 cross-seed passport — implementation map

The fail-closed cross-seed passport that lets one Pantheon seed admit another
seed's agent as an authenticated-but-untrusted counterparty. Ratified in
`architecture/adr/ORG-047-cross-seed-passport-a2a-federated-identity.md`. Built under T-2018
(umbrella) → T-2830 (Layer A core) + T-2832 (concrete adapters).

**Read this before reviewing the crypto** — it states the trust model the code
depends on, so a reviewer doesn't measure it against the wrong one (e.g. expecting
PKI CA-chain validation that is deliberately absent).

## The trust model (the one thing to get right)

The trust anchor is the **counterparty's DID document, pinned by DNSSEC**, NOT a
PKI certificate-authority chain.

1. **D1 step 1 (card-auth, `passport_handshake.py`)** establishes that a DID
   document / Agent Card genuinely belongs to the endpoint: a DNSSEC-signed DNS
   TXT record pins the Agent Card's hash (the TG-869 primitive), and the endpoint
   must be in the local hand-managed allowlist. Absent/invalid DNSSEC, a
   hash-mismatch, or a not-allowlisted endpoint → **ABORT** (never a fallback to
   unsigned DNS).
2. Because the DID document is now DNSSEC-pinned + allowlisted, the keys it
   publishes in its `authentication` verification relationship are trusted to be
   that endpoint's keys.
3. **D1 step 3 (presenter-auth, the adapters)** therefore trusts a presented
   credential iff it proves possession of one of those DID `authentication` keys.
   A client cert or a JWT is trusted because its key **is** the DID-authenticated
   key — not because it chains to a CA. `verify_mtls_binding` and
   `verify_bearer_jwt` both refuse anything not bound to that key.

So: **DNSSEC + allowlist vouch for the DID doc; the DID doc vouches for the keys;
the keys vouch for the presenter.** A CA chain is not part of this chain of
custody and its absence is intentional (ADR D3).

**D4 invariant:** a verified identity gates *who*, never *what* — every admit
carries `content_trusted = False`. Authentication never confers content trust.

## Module map

| module | role | tests |
|---|---|---|
| `passport_handshake.py` | the D1 state machine + verifier (resolve→select-scheme→present→admit-untrusted), scheme precedence + stateful downgrade rejection, the TG-869 card-hash/allowlist primitive, the `SchemeAdapter` interface | `test_a2a_passport_handshake.py` (27) |
| `did_web.py` | did:web URL resolution + DID-document parse + `authentication`-key/alg extraction (foundation both adapters need) | `test_a2a_did_web.py` (18) |
| `passport_mtls.py` | mTLS: presented client-cert public key **==** DID authentication key (+ fingerprint pin). `LiveMtlsAdapter`. | `test_a2a_passport_mtls.py` (11) |
| `passport_bearer.py` | HTTP-Bearer-profile JWT signed under a DID authentication key (sig + iss/aud/exp/nbf-leeway/fresh-jti). `LiveBearerAdapter`. | `test_a2a_passport_bearer.py` (21), `test_a2a_passport_didres.py` (36) |
| `netcaps.py` | ORG-071 shared foundation: `NetcapsParams` (every named bound, validated), the D6 vocabulary (`PEER_ABORT`, `AuditEventType`, `AuditSink` + in-memory/logging sinks), the injectable `Clock` (`MonotonicClock`, `FakeClock`) | `test_a2a_netcaps_params.py` (22), `test_a2a_netcaps_audit.py` (14) |
| `netcaps_timeouts.py` | ORG-071 D3: named step timeouts + total handshake deadline (`TimeoutPolicy`, `HandshakeDeadline`, `StepTimeout`), wired into `PassportHandshake` | `test_a2a_netcaps_timeouts.py` (15) |
| `netcaps_ratelimit.py` | ORG-071 D1 + D5: `RateLimiter` (tiered budgets keyed by verification state, handshake rates + concurrency caps, outbound amplification bounds, weighted cost table), plugged into `PassportHandshake` through the admission seam (`netcaps_admission.py`); `netcaps_breach.py` is the shared `NetcapsBreach` carrier | `test_a2a_netcaps_ratelimit.py` (42) |
| `netcaps_breaker.py` (+ `netcaps_failure_class.py`, `netcaps_breach.py`) | ORG-071 D1 per-anchor-host circuit breaker (`AnchorCircuitBreaker`) as `BreakerTxtResolver` / `BreakerCardFetcher` wrappers; the D2 transient-vs-definitive split (`classify_fetch_failure`); the shared `NetcapsBreach` signal | `test_a2a_netcaps_breaker.py` (52) |
| `netcaps_quota.py` | ORG-071 D4: `QuotaGate`, the per-counterparty quota on the verified DID at `POST_AUTH` (granted / authenticated-ungranted tier from an injected `GrantLookup`; `StaticGrants` in-memory), charged once per verified handshake through `RateLimiter.charge()` | `test_a2a_netcaps_quota.py` (23); `test_a2a_netcaps_hardening.py` (T-5399 PR-a fail-open regressions) |
| `netcaps_cache.py` | ORG-071 D2: DID-doc + status-list cache decorator (`ResolutionCache`) — STRICT freshness, failure-classed negative caching, singleflight, jitter, bounded LRU; raises `NetcapsBreach` (`netcaps_breach.py`) | `test_a2a_netcaps_cache.py` (91) |

All of these are **pure-local** (no network/SDK/clock-injection-needed in a way that
breaks it): every side effect — DNS TXT resolution, card fetch, DID-doc fetch, the
presented cert/token — is injected, so the whole thing runs in the system smoke
(`python3 -m unittest discover -s tools -p 'test_a2a_*.py'`). did_web/handshake use
no third-party libs; mtls uses `cryptography`, bearer uses PyJWT (both
system-available, skip-guarded).

## Scheme precedence (D1 step 2)

`mTLS > OIDC/Bearer > API-key` (a fixed strength order). Step 2 picks the
strongest scheme common to (counterparty-advertised ∩ our-registered-adapters).
**Stateful downgrade rejection**: a scheme advertised on a *prior* handshake with
an endpoint that is now absent, and stronger than the one selected, → ABORT
(high-water-mark memory; a legitimate scheme retirement needs a deliberate
operator reset). VC is kin-gated and out of the linear precedence.

## Network capabilities (ORG-071, T-2857)

The our-side load bounds, enforced uniformly across every scheme adapter
(`architecture/adr/ORG-071-passport-network-capabilities.md`). Two rules shape
the code:

- **A bound breach is an ABORT** (ORG-047 D1 fail-closed), never a degraded
  session. Its internal reason is an `AbortReason` member (the same enum as the
  auth failures), and it emits one named ORG-070 audit event
  (`BOUND_BREACH_EVENTS` in `passport_handshake.py`).
- **The peer cannot tell why** (D6 anti-oracle). `HandshakeResult.peer_error`
  is `PEER_ABORT` for every ABORT, auth failure or bound breach alike. Only
  `abort_reason`, the log and the audit event carry the precise reason.
- **`did_resolution_failed`** (T-5399 PR-b) is a named audit event
  (`AuditEventType`) with a matching `AbortReason`, for a DID document that
  could not be resolved. The D6/E6 event list is open-ended ("etc."), so adding
  a named event is within build latitude (HO ruling 2026-10-23, T-5399).

Timeouts (D3) are always on: `PassportHandshake` defaults to
`TimeoutPolicy()` on the monotonic clock. The DNS/TXT lookup, the Agent Card
fetch (connect + TLS + read) and the presentation round-trip each run inside one
per-handshake deadline; a late result is discarded and the handshake ABORTs.

### DID document resolved inside `run()` (T-5399 PR-b)

`PassportHandshake(did_resolver=...)` takes `(did, *, source, deadline) ->
ParsedDidDocument`, i.e. what `ResolutionCache.did_documents(fetcher)` returns.
An adapter that declares `needs_did_doc` (the `DidDocAdapter` protocol:
`needs_did_doc` plus a stateless `verify_with_doc(endpoint_id, entry,
did_doc)`) has the counterparty DID document resolved at step 3, before
`verify`:

1. the `RESOLVE` gate is consulted again with `AdmissionContext.resource =
   "did_doc"`;
2. the resolver runs under the run's `HandshakeDeadline` (also wrapped in
   `deadline.call`, so a late result is discarded) with the run's `source`
   (`UNKNOWN_SOURCE` when `None`);
3. the `PRESENT` gate and `verify_with_doc` follow.

`LiveBearerAdapter` works both ways: built with a parsed doc (unchanged, no
resolver call) or without one (`needs_did_doc` is True; its plain `verify()`
refuses). With no `did_resolver` the handshake behaves as before.
Failures: `NetcapsBreach`/`CacheBreach` map through their own reason,
`StepTimeout` to the timeout reasons, and a `DidResolutionError`, a wrong-DID
or non-document result, or any other `Exception` to `did_resolution_failed`
(one audit event; only the exception type is in the detail). A generic
`Exception` from `verify`/`verify_with_doc` is `PRESENTATION_FAILED`, logged
with the type only.

The limiter charges the `did_doc` RESOLVE on the same pre-auth keys as the TXT
one (D4: the source, never the claimed DID's authenticated quota) but only the
global rate, the per-(source, anchor) rate and the `DID_RESOLVE` budget; the
per-source unresolved rate and the distinct sets were already counted by the
TXT RESOLVE of the same run. An unknown `resource` value takes the full charge.

**Accepted cost: the global resolution rate is charged twice per handshake**
(TXT plus DID doc), so the shared 120/min `resolution_rate_global` now admits
about 60 handshakes/min. HO ruling 2026-10-23 (T-5399): accepted as correct,
since a handshake makes two outbound lookups. First-fire tuning item (T-2857).
Not yet wired: `LiveMtlsAdapter` still takes a pre-parsed doc.

### Rate limiter and admission seam (T-2857 PR2)

`PassportHandshake(admission=[...])` takes an ordered list of gates
(`netcaps_admission.AdmissionGate`: `check(ctx)` / `release(ctx)`). The list
is empty by default, which means no gating and the old behaviour. Each gate is
consulted at four phases: `START` (before any work), `RESOLVE` (just before
the outbound DNS/TXT lookup), `PRESENT` (before the adapter's `verify()`) and
`POST_AUTH` (after a verified presentation, before admit). The first breach
ABORTs. Every gate that passed `START` is released exactly once, however the
run ends. `POST_AUTH` is where the D4 quota gate (below) runs on the verified DID.

A gate whose `check()` raises (any `Exception`) is a generic bound breach: the
handshake ABORTs as `rate_limit_rejected` with one audit event naming the gate
class and exception type. An audit sink that raises never changes the outcome:
the ABORT is still returned, the failure is logged at ERROR and noted in the
result log. A resolver or card fetcher that raises, or a fetcher that returns
non-bytes, ABORTs as `no_dnssec_txt_record` / `agent_card_fetch_failed`.
`KeyboardInterrupt` and `SystemExit` always propagate.

`run(endpoint_id, *, source=None)` takes the transport identity for the
per-source bounds. `source=None` counts as unknown.

`RateLimiter` puts traffic in one of two classes:

- **Known.** The claim is on an allowlisted DID from a real source. The
  per-source bounds are keyed per source. The pre-auth budget and the
  per-counterparty rate and concurrency bounds are keyed per (source, claimed
  DID), because kin DIDs are public: no source can spend another source's
  pre-auth bounds for a DID it has not yet proven (D4). The per-anchor
  resolution rate is likewise keyed per (source, anchor) for these claims, so
  a distributed attacker can push a kin anchor only up to the global
  resolution rate. Bounds on the verified DID belong to `POST_AUTH` (PR4).
- **Unknown.** Everything else shares one `UNKNOWN_SOURCE` pool: one key in
  every per-source and per-counterparty table, plus the shared
  `budget_unknown_source`. Unknown parties therefore hold at most one source's
  share of each global bound, and they create no per-party state.

**Global caps are shared (accepted trade-off).** The global resolution rate
and the global concurrency cap are shared by every claim, known and unknown,
so a distributed attacker with enough sources can still fill them and delay
known counterparties. Reserved headroom for known claims is a first-fire
tuning item, not built.

Excess is rejected and never degraded. A check is atomic, so a refused check
consumes nothing. Every keyed table holds at most `max_tracked_keys` keys
(default 1024). When a table is full, its expired windows are evicted first.
If it is still full, the new key is refused (fail-closed).

Breaches travel as `NetcapsBreach(reason, event, detail)`. Gates return one,
and the PR3/PR5 resolver and fetcher wrappers raise one. The handshake maps it
onto `AbortReason` and `_breach()`, and is the single emitter of the refusal
audit event. An unmapped reason fails closed as `rate_limit_rejected`.

D5 weights (`DEFAULT_COSTS`, provisional) are 1 unit for a handshake start, a
token check or a DID resolve; 2 for a signature verify; 4 for a status-list
decode; and 8 for a VC verify. A deployment or adapter may raise a weight
(`stricter_costs`) but never lower one. A scheme with no named op is charged
as the dearest op.

### Per-anchor circuit breaker (D1)

`netcaps_breaker.py` keeps one breaker per **anchor host** (the host actually
contacted), never per counterparty DID, so a hostile party can only trip the
breaker of a host it can make fail. Wire it by wrapping the injected
dependencies: `BreakerTxtResolver(resolver, br)` and
`BreakerCardFetcher(fetcher, br)`. `passport_handshake.py` is unchanged.

The key is canonical: lower-case, no trailing dot, no default port, IPs in
compressed form with IPv6 bracketed (`[::1]:8443`), an IPv4-mapped IPv6
literal as its IPv4 form, and a Unicode host as its `xn--` A-label (stdlib
IDNA 2003 codec; a Unicode host the codec maps beyond case-folding, such as
`ß`, fullwidth forms, ZWJ or final sigma, is refused, because an IDNA 2008
resolver would reach a different host; a bad `xn--` label is refused). A
percent-encoded host and non-dotted / shorthand / octal / hex IPv4 (`2130706433`,
`0x7f.0.0.1`, `127.1`, `0177.0.0.1`) are refused. The resolver-to-fetcher
anchor context is a `ContextVar`, so it holds across threads and asyncio tasks.
If the audit sink raises while the breaker emits its open event, the state
still updates and the error is logged at ERROR. Each host has two entries, because they are different
infrastructure: the TXT lookup (`dns_key(host)`) and the card fetch (`host`).
A card fetch counts a failure only when the card URL's host is the anchor of
the DID being resolved. So a kin whose TXT points at another kin's host cannot
trip that host's breaker, though an already-open breaker there still gates the
fetch. Known limitation: a kin whose Agent Card is hosted off its DID host
therefore never counts HTTPS failures toward a breaker.

- **closed**: consecutive transient failures are counted. At
  `circuit_breaker_threshold` the breaker opens.
- **open**: every request to that anchor raises `NetcapsBreach`
  (`anchor_circuit_breaker_open` / `circuit_breaker_open`) without touching the
  network, for `circuit_breaker_cooldown`. The handshake hook that maps the
  breach records the refusal. The breaker itself emits one
  `circuit_breaker_open` audit event only on each open and reopen.
- **half-open**: after that, one recovery probe is let through, at most one
  per `circuit_breaker_probe_interval`. The anchor answering closes the
  breaker; a transient failure reopens it until the next probe slot.

Only **transient-infra** failures count (`netcaps_failure_class.py`): an
`OSError` (timeouts, DNS, connect, TLS), `TransientAnchorError` (a concretion
raises it for 429 / 5xx), or dnspython's `dns.exception.Timeout`. A returned
value, even a `None` card or an absent TXT, means the anchor answered, so it is
**definitive** and resets the count. Any other exception is `UNKNOWN` and
neutral. An anchor that answers intermittently therefore never trips. That is
accepted: it is usually the counterparty's own host, and the D3 timeouts and
the per-anchor resolution rate bound its cost. Otherwise
the wrappers are transparent: they return or re-raise exactly what the inner
callable did.

Memory: a healthy anchor holds no entry, and at most `MAX_TRACKED_ANCHORS`
(1024) are tracked. A new anchor is never refused for lack of room. When the
table is full, the victim is the least-recently-touched closed entry, else the
oldest open entry whose probe slot has passed, else the oldest entry. Losing an
open breaker only means no breaker for that host, which stays bounded by the
D3 timeouts and the per-anchor resolution rate. Refusing healthy anchors
instead would let wildcard DNS fill the table and deny the whole federation.

### Per-counterparty quota (T-2857 PR4)

`QuotaGate(limiter, grants)` is the D4 quota on the post-auth key, the
verified DID. Add it after the limiter, e.g.
`admission=[limiter, QuotaGate(limiter, grants)]`. It acts only at
`POST_AUTH`, after proof of possession, and is a no-op at every earlier phase.
Pre-auth load under a claimed DID is charged to the (source, claimed DID)
pre-auth budget, and a handshake that ABORTs before `POST_AUTH` never reaches
the quota, so neither can drain a victim's quota.

The tier comes from the injected `GrantLookup.grant_quota(did)` (ORG-069's grant
store can back it later). An allowlisted DID with a grant gets `Tier.GRANTED` with
the grant's quota. `StaticGrants.granted(dids, params)` gives each DID
`budget_granted`. Any other verified DID gets `Tier.AUTHENTICATED_UNGRANTED` with
`budget_authenticated_ungranted`. An invalid quota (negative, non-finite, non-numeric, a bool,
or too large to represent) is refused, and so is a `grant_quota` lookup that
raises; both are `quota_exhausted` ABORTs (fail-closed).

**The single charge:** `QuotaGate.check` at `POST_AUTH` calls
`RateLimiter.charge()` once per verified handshake, for `Op.HANDSHAKE_START`'s
weight. The limiter's own `check()` charges no post-auth tier. The windows, the
atomic refusal and the bounded-table rule are the limiter's. At most
`max_tracked_keys` DIDs are tracked per tier, and a new DID arriving at a full
table is refused. Only verified, allowlisted DIDs reach the gate, so the key set
is bounded by the allowlist anyway. The cap stays as a backstop.

Exhaustion is a throttle. The gate returns a `quota_exhausted` breach, and the
handshake ABORTs at `admit_untrusted` and emits the one audit event. Nothing
touches grant or allowlist state, and the next window admits again.

### Resolution + status cache (D2)

`ResolutionCache` in `netcaps_cache.py` decorates a network-touching
`RawFetcher(url, *, max_bytes, timeout) -> FetchResponse`. Use
`cache.did_documents(fetcher)` for DID documents and
`cache.status_lists(fetcher, validator)` for status lists.
`passport_handshake.py` does not change.

- **Positive entries hold only validated material.** Each entry is keyed by
  the identity it was fetched for: the DID for a DID document, and issuer + URL
  for a status list. An entry fetched for one identity is never served for
  another. The TTL is min(Cache-Control max-age, local max), and jitter only
  shortens it.
- **STRICT freshness.** Expired material is never served. A miss or a failed
  refresh raises a `NetcapsBreach` (`CacheBreach`).
- **Failures are classified at the fetcher boundary.**
  - *Definitive-absent*: 404/410 via `FetchFailure`, an oversized body, or an
    explicit validator rejection (`ValueError` / `DidResolutionError`, or a
    `CacheBreach` marked definitive). It is cached negatively for
    `negative_ttl_definitive_absent`, keyed by the identity, and a later hit
    reports `negative_cache_hit`.
  - *Transient-infra*: DNS/TLS, 429/5xx and timeouts, as classified by PR5's
    shared `classify_fetch_failure` (`netcaps_failure_class.py`). These only
    suppress fetches, for `negative_ttl_transient_infra`, keyed by anchor
    host + source and never by the counterparty. Fresh positive material is
    still served during the window.
    A short body (`declared_length` mismatch) is transient too. When a
    validator hits a transient error, the cache suppresses only the host that
    actually failed: a nested `CacheBreach` carries that host as `anchor`. A
    bare timeout or OSError names no host, so nothing is suppressed.
  - *Unknown* exceptions (`FailureClass.UNKNOWN`), from the fetcher or the
    validator, abort and leave no negative state, so a bug cannot become a
    DID-scoped denial window.
  - A validated success clears both kinds of negative entry.
- **Breach reason/event strings** are PR1's `AbortReason` and
  `AuditEventType` values; the module docstring has the full table. The cache
  emits no audit event itself, because PR2's hook maps `NetcapsBreach` to
  `_breach()`, which emits one. A failed DID-doc fetch with nothing held (and
  an unresolvable DID) reports `did_resolution_failed`; an expired held entry
  still reports `stale_material_denied`.
- **Singleflight.** A lock plus per-key in-flight events, which matches the
  synchronous handshake. No caller waits past its own deadline:
  - The leader hands the fetcher `timeout=` min(full D3 per-fetch budget, its
    remaining time), and a waiter waits min(full budget, its remaining time).
  - A timeout on a budget the leader's deadline cut is that caller's alone.
    It suppresses nothing and is not shared. Waiters that still have budget
    retry as the new leader, at most `MAX_FLIGHT_ATTEMPTS` times, then do ONE
    solo fetch on their own budget (not a shared flight, so no other caller's
    deadline can cut it): at most one extra fetch per caller.
  - "Cut" means a budget below `MATERIAL_CUT_RATIO` (0.9, provisional) of
    the full budget. A nested resolve inside a validator that hit its own
    caller's deadline stays deadline-only and suppresses nothing.
  - A timeout on a budget at or above that ratio is a real anchor timeout. It is shared with
    the waiters and suppresses the anchor.

  The fetcher must honour `timeout=`. The cache does not pre-empt it.
- **Bounded memory.** One LRU (`max_entries`, default 1024) holds positive,
  negative and suppression entries together.
- **Size caps.** The fetcher receives `max_bytes` and must stop reading at
  `max_bytes + 1`; the cache re-checks it. The fetcher must also detect a
  short body (Content-Length or chunked mismatch) and raise OSError or
  `TransientAnchorError` rather than return truncated bytes. `declared_length`
  is the RAW on-the-wire length and `body` the raw undecoded bytes, so the
  fetcher must send `Accept-Encoding: identity` and must not content-decode
  (a decoded body against a raw length reads as a short body, transient). The decompressed and ratio caps
  apply inside the status-list validator through `bounded_gunzip` (Layer B
  parses StatusList2021).
- **Deadline seam.** Each resolve call accepts `deadline=` (any object with
  `remaining()`), which refuses a spent deadline and discards a late result for
  that caller. `PassportHandshake` resolves DID docs inside `run()` and passes
  the run's `HandshakeDeadline` (see "DID document resolved inside `run()`").

### Parameter defaults

**Provisional first-fire defaults; tuning is the operator's value-decision (ORG-071
D3, T-2857).** The ADR names the parameters and carries no numbers. These lean
conservative for a small federation of hand-allowlisted kin, so first fire
errs toward refusing load. The table is rendered by
`netcaps.parameter_table()`, and a test fails if it drifts from the code.

Construction-time rules: no negative, non-finite or non-numeric value; zero
only for `jitter_range_pct`, `max_redirects` and `retry_policy`; the positive
(and negative) TTLs ≤ the ORG-047 D5 rotation ceiling `ROTATION_CEILING_S` =
900 s; every step timeout ≤ `timeout_total_handshake`; per-source and
per-counterparty caps ≤ their global cap; `budget_preauth` ≤
`budget_authenticated_ungranted` ≤ `budget_granted`; decompressed status-list
cap ≥ compressed cap; `max_compression_ratio` ≥ 1; jitter ≤ 50%.

Budgets and the distinct-DID/anchor caps count per accounting window,
`ACCOUNTING_WINDOW_S` = 60 s. That is a fixed constant, not a taxonomy
parameter. Budget units are D5 weighted-cost units, where 1 unit is one cheap
token check.

| name | group | default | unit | rationale |
|---|---|---|---|---|
| `did_doc_positive_ttl_max` | Caching | 300 | s | positive DID-doc cache cap; well under the ORG-047 D5 rotation ceiling |
| `status_list_positive_ttl_max` | Caching | 300 | s | positive status-list cache cap; revocation freshness same as rotation |
| `negative_ttl_definitive_absent` | Caching | 60 | s | short class-TTL for 404/410/malformed/bad-sig, scoped to the DID |
| `negative_ttl_transient_infra` | Caching | 5 | s | brief advisory fetch-suppression only, never handshake denial |
| `jitter_range_pct` | Caching | 10 | % | refresh jitter against expiry stampedes; 0 disables, max 50 |
| `timeout_dns` | Timeouts | 5 | s | DNS/TXT lookup; a cold DNSSEC-validating lookup fits |
| `timeout_connect` | Timeouts | 3 | s | TCP connect |
| `timeout_tls` | Timeouts | 3 | s | TLS handshake |
| `timeout_fetch_read` | Timeouts | 5 | s | per-fetch read (Agent Card, DID doc, status list) |
| `timeout_handshake_roundtrip` | Timeouts | 10 | s | one presentation request/response round-trip |
| `timeout_total_handshake` | Timeouts | 20 | s | wall-clock cap on the whole exchange; every step timeout must fit |
| `max_conns_global` | Connections | 64 | conns | open TCP/TLS connections, all sources |
| `max_conns_per_source` | Connections | 4 | conns | open connections from one source |
| `max_concurrent_handshakes_global` | Connections | 16 | handshakes | in-flight handshakes, all sources |
| `max_concurrent_handshakes_per_source` | Connections | 4 | handshakes | in-flight handshakes from one source (a kin seed's personas share one IP) |
| `max_concurrent_handshakes_per_counterparty` | Connections | 2 | handshakes | in-flight handshakes for one counterparty |
| `rate_handshake_per_source` | Connections | 30 | per min | handshake starts from one source |
| `rate_handshake_per_counterparty` | Connections | 10 | per min | handshake starts for one counterparty |
| `budget_preauth` | Budgets | 20 | units/window | pre-auth work per source; the primary DoS target, so the tightest tier |
| `budget_authenticated_ungranted` | Budgets | 50 | units/window | verified but no ORG-069 grant yet |
| `budget_granted` | Budgets | 200 | units/window | default grant quota (D4) absent an override |
| `budget_unknown_source` | Budgets | 100 | units/window | isolated shared pool for all unknown sources, cannot starve known kin |
| `budget_known_degraded` | Budgets | 20 | units/window | known counterparty during its own outage |
| `resolution_rate_global` | Amplification | 120 | per min | all outbound DID/status resolutions |
| `resolution_rate_per_source_unresolved` | Amplification | 10 | per min | uncached resolutions one source can trigger |
| `resolution_rate_per_anchor` | Amplification | 30 | per min | outbound fetches to one anchor host |
| `max_distinct_dids_per_source_window` | Amplification | 16 | DIDs/window | distinct DIDs one source may present (caching cannot bound these) |
| `max_distinct_anchors_per_source_window` | Amplification | 3 | hosts/window | distinct anchor hosts one source may name |
| `circuit_breaker_threshold` | Amplification | 5 | failures | consecutive transient failures that open it |
| `circuit_breaker_cooldown` | Amplification | 60 | s | open time before the first recovery probe |
| `circuit_breaker_probe_interval` | Amplification | 30 | s | gap between recovery probes while still failing |
| `max_did_doc_bytes` | Sizes | 65536 | bytes | a DID doc is a few KiB; 64 KiB is ample |
| `max_status_list_bytes` | Sizes | 65536 | bytes | compressed StatusList2021 credential on the wire |
| `max_status_list_decompressed_bytes` | Sizes | 1048576 | bytes | 1 MiB bitstring = 8M entries; decompression-bomb cap |
| `max_compression_ratio` | Sizes | 1024 | x | empty bitstrings compress about 350x, so the cap sits above that |
| `max_negotiation_steps` | Sizes | 4 | steps | message exchanges per handshake |
| `max_redirects` | Sizes | 0 | redirects | did:web fetches follow none (SSRF/amplification) |
| `retry_policy` | Sizes | 0 | retries | retries per fetch, transient-infra only; 0 = never |

## What to adversarially probe (the T-2831 review surface)

The reject paths are the security property. Confirm each **refuses**, and that a
subtle bug could not turn it into an accept:

- **mtls**: a valid cert for a *different* key (the bypass class); a cert whose
  key is the right value but wrong fingerprint; an alg the DID doc doesn't publish
  for authentication; RSA where EC is pinned; malformed cert bytes. Scrutinise the
  EC-JWK→key reconstruction (`_b64url_to_int`, curve mapping) and the SPKI-DER
  byte-equality — a canonicalisation slip there is an auth bypass.
- **bearer**: bad signature; expired / not-yet-valid; wrong `aud`; wrong `iss`;
  replayed `jti`; alg-pin mismatch; a `kid` that names a verificationMethod NOT in
  the `authentication` relationship; a token missing a required claim. Confirm the
  header alg is pinned *before* the signature is trusted, and that `require`
  forces exp/iss/aud/jti presence.
- **did_web**: an authentication reference that doesn't resolve; duplicate `kid`;
  a document `id` ≠ the resolved identity; empty authentication; ambiguous
  alg-key selection.
- **handshake core**: every step-1/2 ABORT; the downgrade-memory record-on-success
  (never on abort); the fail-closed-by-construction property (no real adapter
  registered ⇒ selects a scheme but cannot admit).

## Done vs deferred

**Done (pure-local, adversarially tested):** the D1 handshake core, did:web
resolution, and both concrete D3 adapters (mTLS + Bearer), composing end-to-end.

**Remaining in T-2832:** (4) the live TLS-session plumbing that hands
`LiveMtlsAdapter` a real peer cert (deployment integration into the A2A serving
substrate — not pure-local); RSA key support (currently EC-only); true OIDC/OAuth2
(D3 case b — external IdP discovery, "built when a counterparty needs it").

**Deferred out of this arc:** the credential/key **values** — key-custody model,
seat-cred TTL, deny-list, StatusList2021 revocation (T-2803 / T-2810 / T-2811);
and the operator-gated **publish** of the apex DNS + public `/.well-known/did.json`
(Layer C). The code here is additive/removable (ADR line 89) — nothing is a
one-way door until a public Agent Card is published.
