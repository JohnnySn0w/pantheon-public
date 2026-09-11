---
symptom: "A value produced by service A and validated by service B fails closed in production even though A emitted it legitimately — because B validates it against its own length/size constant that is TIGHTER than the largest value A can legitimately produce (A's wire limit). Unit tests on each service pass; only exercising the two services against each other reveals it. Concrete instance: approval-domain MAX_PLAN_SUMMARY_LENGTH=1024 refused the broker's real 3134-byte plan summary, so HostApproval1.Review would fail closed with 'invalid plan summary' on EVERY real plan (T-2913, defect landed by the T-2910/T-2911 tier, latent until the cross-service integration test ran)."
fix: "Bound a cross-service value at the consumer by what the PRODUCER can legitimately emit — i.e. the producer's wire limit (here the shared wire _JSON_LIMIT=65536) — not an independent, tighter, locally-guessed constant. Then pin the relationship with a regression test (consumer_bound >= producer_wire_limit, or == the shared wire constant) so a future re-tightening fails loudly instead of rediscovering the outage in production. DISTINGUISH from caller-supplied inputs: a tight bound on data a CALLER passes in (anchors, targets, handles) is correct defensive validation, NOT this bug — the bug is specific to data FETCHED FROM ANOTHER SERVICE."
domain: "cross-service, ipc, dbus, validation, length-bound, fail-closed, host-control, action-broker, integration-test, pantheon"
---

# A cross-service value must be bounded at the consumer by the producer's wire limit, not an independent guess

**Class:** cross-service contract mismatch, latent-until-integrated. Each service's unit tests pass because each only sees its own fixtures; the defect lives in the *seam*.

## The shape

Service A produces a value (a summary, a serialized record, a payload) and sends it over a wire with a limit `L_wire`. Service B fetches that value and validates it against a local constant `L_domain`. If `L_domain < L_wire`, then any legitimately-produced value in `(L_domain, L_wire]` makes B reject A's valid output — a fail-closed outage that no single-service test catches.

The tell: `L_domain` is a hand-picked "feels big enough" number (1024) while the data's real ceiling is the wire limit (65536). A domain bound *tighter than the wire it consumes from* is the bug.

## Why unit tests miss it and what catches it

Per-service unit tests use fixtures sized to that service's imagination. The real producer's output size is only observed when the two services run against each other. In T-2913 the catch came from a cross-service integration test that had been marked **severable** in planning — it earned its place precisely by exercising the seam. **Lesson: a cross-service integration test is not optional polish; it is the only test that can find this bug class.** Don't drop the seam test to save a rebound cycle.

## Fix

1. Set the consumer's bound from the producer's wire limit (share the constant if both live in the same package, as `wire._JSON_LIMIT` does here).
2. Add a regression test pinning `consumer_bound >= producer_wire_limit` (or `==` the shared wire constant) so a future tightening fails loudly.
3. Comment the bound naming the alignment, so the next reader sees it is a derived constraint, not a free choice.

## Not this bug

Tight bounds on **caller-supplied** inputs (D-Bus method arguments, user handles, anchors) are correct defensive input validation — bound those as tightly as the input spec allows. The bug is only for data one service FETCHES from another; the producer, not the consumer, sets the legitimate ceiling.

## Verification

`grep -rn -iE '(MAX|LIMIT|_LEN|_BYTES).*= *[0-9]' tools/host_control/ --include='*.py' | grep -v test_` — enumerate domain bounds, then for each ask "is this data produced by another service?" If yes, confirm the bound >= that producer's wire limit. In T-2913 this swept clean after the fix: MAX_PLAN_SUMMARY_LENGTH was the unique cross-service instance (now 65536 = wire _JSON_LIMIT); all other tight bounds validate caller inputs.
