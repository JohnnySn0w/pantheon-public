# ORG-047 cross-seed passport — implementation map

The fail-closed cross-seed passport that lets one Pantheon seed admit another
seed's agent as an authenticated-but-untrusted counterparty. Ratified in
`adr/ORG-047-cross-seed-passport-a2a-federated-identity.md`. Built under T-2018
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
| `passport_bearer.py` | HTTP-Bearer-profile JWT signed under a DID authentication key (sig + iss/aud/exp/nbf-leeway/fresh-jti). `LiveBearerAdapter`. | `test_a2a_passport_bearer.py` (15) |

All four are **pure-local** (no network/SDK/clock-injection-needed in a way that
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
