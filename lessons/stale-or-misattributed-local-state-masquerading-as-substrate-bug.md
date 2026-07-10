---
title: "Stale or misattributed local state masquerading as a substrate/vendor bug"
date: 2026-06-09
authors: orchestrator
status: authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
---

**Re-evaluate when:** a 5th instance surfaces (strengthens the case for promoting the falsifying-test-first discipline into a D-class directive or a pre-flight checklist hook), OR a structural enforcement becomes available that forces "rule out local state first" at the point a substrate-bug conclusion is about to be reported (e.g., a reviewer/orchestrator pre-flight prompt), OR this lesson gets cited in a design conversation (a 2nd citation promotes "useful in theory" → "load-bearing in practice").

## Lesson

When a symptom *looks like* a substrate or vendor bug — the API is broken, the network is blocking us, the harness ignores our config, the daemon the platform manages has failed — the prior probability that the real cause is **our own stale or misattributed local state** is high enough that **ruling out local state must come first**, before any "the substrate is broken" conclusion is reported upward or acted on.

This is the diagnostic dual of [[stacking-mechanisms-may-mean-the-substrate-is-the-problem]] (which says: when you're stacking workaround on workaround, maybe the substrate genuinely *is* the problem). Both are about the local-vs-substrate attribution boundary; this one names the more common and more expensive error — **blaming the substrate for what turns out to be a dead key, a stale process, a misconfigured path, or a local hook.** The cost is asymmetric: a substrate-bug conclusion tends to *stop* investigation ("nothing we can do, it's their bug / the network / the platform"), so a wrong one buys days of stalled status rather than minutes of wasted probing.

## Why it recurs (the trap mechanism)

A substrate/vendor explanation is *attractive* for reasons unrelated to its truth:

- It's **exculpatory** — the fault is external, not in our setup. There's no pull to scrutinize it the way we'd scrutinize "I misconfigured something."
- It **terminates the search** — "it's their bug" is a stopping state; you file it and move on, so the wrong conclusion doesn't get the second look a still-open question would.
- The **failure surface often collapses distinct causes to one signal** — a vendor auth layer that returns `500` for *both* "revoked key" and "real outage"; a `systemd` `oneshot` that reports `active (exited)` whether the spawned process lives or died; a `git push` that hangs identically for "network down" and "local hook wedged." When the signal can't distinguish your-fault from their-fault, the brain fills the gap with the exculpatory option.

The antidote is to make the **local-state hypothesis cheap to falsify and to run that test first** — before the substrate conclusion is allowed to leave your own head.

## Instances (the pattern, four ways)

1. **j17 dead key returns 500 → "vendor deploy bug, still blocked" (5 days).** Every auth'd probe against the j17 deploy returned `HTTP 500`; read as "Bug 2 is substrate-wide, Elija's deploy hasn't landed." Actual cause: the Telegram-relayed test key was revoked; a *known-good key from `echo-prismatic/.env`* passed all 4 live tests in 8s. The deploy had been live the whole time. (See [[dead-key-sharp-edge-j17-revoked-key-returned-500]] for the key-hygiene angle; this lesson is the diagnostic angle.)

2. **`~/.claude/` files bypass per-file allow rules → "my allow-rules are broken" (substrate-config confusion).** HANDOFF.md edits kept getting blocked despite explicit `Edit`/`Write` allow rules in `settings.json`. Read as a broken/ignored config. Actual cause: a Claude Code dir-scoping behavior — files under `~/.claude/` are exempt from per-file allow rules. Fixed by relocating HANDOFF.md out of `~/.claude/`, not by fighting the config. (See `bugs/claude-code-dot-claude-dir-bypasses-per-file-allow-rules.md`.)

3. **kino zombie process → personal daemon misidentified twice.** During the 2026-05-21 Recovery silent-outage recovery, a *separate* `-p` headless kino-agent was process-alive but had `--resume`'d a session that had died days earlier with `API Error: 529`. Because that zombie was alive in `ps`, the recovery agent conflated it with Recovery's personal daemon and misdiagnosed the live state twice before the two-daemon topology was clarified. Stale process *liveness* masqueraded as the daemon being up. (See `incidents/2026-05-21-recovery-personal-daemon-silent-outage/`.)

4. **`git push` hangs, `curl` works → "the network blocks git" (push-misdiagnosis, 2026-06-09).** A `git push` to the pantheon-config remote hung while `curl` to the same host returned fine; concluded the network was blocking git transport. Actual cause: a local `pre-push` hook. The split (curl-works-but-git-hangs) is a *hook* signature, not a *network* one; the single falsifying test `git push --no-verify` would have isolated it in one shot. A working workaround had been found, which should have *raised* scrutiny of the network theory, not settled it. (Memory pins: [[feedback_isolating_test_before_root_cause_claim]], [[feedback_verify_negative_claims_from_logs]].)

## Discipline (forward)

Before reporting or acting on any "the substrate / vendor / network / platform is broken" conclusion:

1. **Run the one falsifying test that isolates local from substrate — first.** A known-good credential from a *different source*. `git push --no-verify`. A probe with a deliberately-malformed input to see if the error signal even discriminates. Ground-truth the datastore rather than inferring from a log-pattern absence. The test should be the *cheapest thing that could prove you wrong*, and it runs before the conclusion leaves your head.

2. **A working workaround RAISES root-cause scrutiny — it does not settle it.** "I found a way around it" means you now have *two* behaviors to explain (why the first failed, why the second worked); the delta between them usually fingerprints the real cause. Treat a successful workaround as new evidence to explain, not as case-closed.

3. **Distrust signals that collapse distinct causes.** If the same `500` / `active (exited)` / hang can mean both your-fault and their-fault, the signal carries no attribution information — get a second signal that does (compare error bodies, check the spawned process not the launcher, diff the working vs failing invocation) before attributing.

4. **State the provenance + freshness of what you probed.** "Probed with key X (source Y, age Z) → 500; cross-checked key A (source B) → 200" makes the stale-local-state cause visible in the record and prevents the next agent from re-running the same stalled loop.

## Related

- [[stacking-mechanisms-may-mean-the-substrate-is-the-problem]] — the dual lesson (sometimes it really is the substrate).
- [[dead-key-sharp-edge-j17-revoked-key-returned-500]] — instance 1, key-hygiene framing.
- [[orchestrator-tier-substrate-verification]] — orchestrators verify substrate claims rather than relaying them.
- `bugs/claude-code-dot-claude-dir-bypasses-per-file-allow-rules.md` — instance 2.
- `incidents/2026-05-21-recovery-personal-daemon-silent-outage/` — instance 3.
- Memory pins: `feedback_isolating_test_before_root_cause_claim`, `feedback_verify_negative_claims_from_logs` — instance 4 + the standing discipline.
- T-0826 — the ORG-012 sweep item that called for this lesson (HO took it as lessons-author per ORG-012; the greenlight step was unnecessary for by-the-book lessons content).
