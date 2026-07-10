---
title: "Project-state hygiene — keys in ONE place, project-progress docs kept up. (j17 dead-key incident as the trigger)"
date: 2026-05-15
authors: recovery
status: authored (lessons/ is an Authored, non-ratifiable class per ORG-015; the older "awaiting ratification" convention predated it)
---

## Lesson (the operator msg_1391 framing)

**Two discipline rules for any project Pantheon orchestrates:**

1. **A project's keys (API tokens, secrets, instance URLs) live in ONE canonical place.** Pick one source — typically the project's own `.env` file (e.g., `echo-prismatic/.env`). Don't ALSO record them in pantheon-config tickets, ledger entries, transition logs, or HANDOFF files where they'll diverge. If the canonical key gets rotated, only one place to update; if a probe goes weird, only one place to check. The 2026-05-15 j17 incident: a Telegram-relayed key landed in T-0185/T-0188 transition logs as "the test key," but the WORKING key was in `echo-prismatic/.env` the whole time. Five days of stale-probe-loop because Recovery kept reaching for the transition-log key. (Note: tickets are append-only audit trail — once a key lands there, you can't strip it; the rule is "don't put it there in the first place.")

2. **Project-progress documentation gets kept up alongside ticket completions.** Each project (echo-prismatic, kino, etc.) should have ONE narrative tracker (BACKLOG.md / PROGRESS.md / similar) that updates as ticket arcs close. The shape: index of arcs/phases shipped + narrative of why/how/what's next. Without it, the ticket history is the only state-of-the-project surface, and ticket history is too granular to reason about arcs cleanly. T-0361 files the structural ask for echo-prismatic; the discipline applies to every project.

These two rules are kissing cousins of the same pattern: **canonical sources, kept current**. When state-of-the-project lives in N places that diverge, every operational question becomes "which source do I trust?" and the answer is empirical (probe each one), which is exactly the loop tonight's incident exemplified.

## The 2026-05-15 incident — five days of stale probes (concrete trigger)

The lesson's narrow technical sub-case — vendor returns 500 instead of 401 on revoked-key auth — is real and worth flagging vendor-side (j17/Elija). But the broader meta-cause was state-hygiene: the working key wasn't where Recovery looked, because Pantheon hadn't picked a canonical key location for echo-prismatic.

T-0133/T-0185 lineage tracked j17 deploy of bugfixes. From 2026-05-08 forward, Recovery + HO ran periodic probes against `https://echo-test.j17.dev` to verify the deploy. The key in scope was the one the operator relayed via Telegram msg_922 on 2026-05-10 (`j17_0_test_pC4h5...`), captured in T-0185 + T-0188 transition logs as authoritative.

Every probe returned `HTTP 500 / {"error":"Internal Server Error","ok":false}` on auth'd endpoints. Recovery's reading: "Bug 2 is substrate-wide; Elija's deploy hasn't landed; T-0185 stays blocked." The unauth path moved to clean 401 responses on 2026-05-14 — partial movement that reinforced the "deploy is in progress, auth path still pending" narrative.

On 2026-05-15 evening, the operator's pre-bedtime nudge "I thought I gave you a working key … or two test keys actually" prompted Recovery to grep for other keys. Found a SECOND key in `/home/claude/echo-prismatic/.env`: `j17_0_test_9cvVe...`. Ran the cargo live tests with it: **all 4 pass in 8.15s, end to end clean**. Elija's deploy was live the whole time. The msg_922 key had been revoked (or was never the right key for this instance), and j17's auth layer returns 500 for revoked-key bearers instead of 401/403.

## What we paid

- ~5 days of "T-0185 still blocked" status reports based on stale-key probes
- A wrong-direction T-0359 cargo verification report (initial run with wrong env-var name) that I corrected to a wrong-conclusion report (wrong key) before the operator's nudge surfaced the real situation
- The PWA-down 2026-05-14 incident that ate ~30min of triage was adjacent symptom-confusion (different stale-key class — Tailscale serve looking like it disappeared because of a status-command surface bug) — same root pattern: stale-state diagnostic looks like substrate bug

## Discipline (forward)

When a vendor endpoint returns 500-class errors on every auth'd call after previously working:

1. **First-check: key validity.** Try a known-good key from a different source (.env file, KMS, sibling project repo). If it works, the prior key is dead — the substrate is fine.
2. **Second-check: surface fingerprint.** Compare the 500 body against a deliberately-malformed-key request. If they're identical (same body, same headers), the auth layer is collapsing all auth failures to 500 — that's a vendor bug worth flagging in your next outreach, but unrelated to whether your test key is alive.
3. **Document in transition log:** "Probed with key X (source Y, freshness Z); got 500. Cross-checked with key A (source B, freshness Z2); got 200." Two-key probe with explicit provenance prevents the entire stale-probe loop.

For Pantheon specifically: when a key lives in BOTH a Telegram-relayed transition log AND a project repo's .env file, the .env is the canonical source-of-truth (it's what the live tests actually use). Telegram-relayed keys may be partial/staging/older. **Cross-check both before declaring substrate state.**

## Vendor-side surfacing

The j17 surface bug ("revoked key returns 500 not 401") is real and worth the operator's next outreach to Elija. A 500 on revoked-key bearer leaks no information back to the API consumer about why the request failed; consumers are forced into "substrate bug or my fault?" guesswork. The fix is trivial on the vendor side (auth middleware should return 401 with a clear `revoked_key` code or similar). Consider raising in the next vendor comm cycle.

## Related

- T-0133 — original bug filing
- T-0185 — verification ticket, closed 2026-05-15 with revised verdict
- T-0188 — earlier Bug 2 smoke (probed with msg_922 dead key, got 500, concluded "still bugged" — same stale-probe pattern)
- T-0359 — the cargo live-test verification that surfaced the second key
- bugs/claude-code-dot-claude-dir-bypasses-per-file-allow-rules.md — sibling sharp-edge from 2026-05-13 (different domain, same shape: stale-state diagnostic that looked like substrate bug)
